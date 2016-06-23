# -*- coding: utf-8 -*-
import json
from pathlib import Path
from typing import Dict, Tuple, Union, Optional, List, Iterator

import abc
import joblib
import tqdm
import numpy as np
import scipy.sparse as sp
import networkx as nx
from formasaurus.utils import get_domain
import scrapy
from scrapy.http import TextResponse, Response
from scrapy.statscollectors import StatsCollector

from deepdeep.queues import (
    BalancedPriorityQueue,
    RequestsPriorityQueue,
    score_to_priority,
    priority_to_score, FLOAT_PRIORITY_MULTIPLIER)
from deepdeep.scheduler import Scheduler
from deepdeep.spiders._base import BaseSpider
from deepdeep.utils import set_request_domain
from deepdeep.qlearning import QLearner
from deepdeep.utils import log_time
from deepdeep.vectorizers import LinkVectorizer, PageVectorizer
from deepdeep.goals import BaseGoal


class QSpider(BaseSpider, metaclass=abc.ABCMeta):
    """
    This spider learns how to crawl using Q-Learning.

    Subclasses must override :meth:`get_goal` method to define the reward.

    It starts from a list of seed URLs. When a page is received, spider

    1. updates Q function based on observed reward;
    2. extracts links and creates requests for them, using Q function
       to set priorities

    """
    _ARGS = {
        'double', 'use_urls', 'use_pages', 'use_same_domain',
        'eps', 'balancing_temperature', 'gamma',
        'replay_sample_size', 'steps_before_switch',
        'checkpoint_path', 'checkpoint_interval',
        'baseline',
    }
    ALLOWED_ARGUMENTS = _ARGS | BaseSpider.ALLOWED_ARGUMENTS
    custom_settings = {
        # 'DEPTH_LIMIT': 100,
        'DEPTH_PRIORITY': 1,
    }
    initial_priority = score_to_priority(5)

    # whether to use URL path/query as a feature
    use_urls = 0

    # whether to use a 'link is to the same domain' feature
    use_same_domain = 1

    # whether to use page content as a feature
    use_pages = 0

    # use Double Learning
    double = 1

    # probability of selecting a random request
    eps = 0.2

    # 0 <= gamma < 1; lower values make spider focus on immediate reward.
    gamma = 0.4

    # softmax temperature for domain balancer;
    # higher values => more randomeness in domain selection.
    balancing_temperature = 1.0

    # parameters of online Q function are copied to target Q function
    # every `steps_before_switch` steps
    steps_before_switch = 100

    # how many examples to fetch from experience replay on each iteration
    replay_sample_size = 300

    # current model is saved every checkpoint_interval timesteps
    checkpoint_interval = 1000

    # Where to store checkpoints. By default they are not stored.
    checkpoint_path = None  # type: Optional[str]

    # Is spider allowed to follow out-of-domain links?
    # XXX: it is not enough to set this to False; a middleware should be also
    # turned off.
    stay_in_domain = True

    # use baseline algorithm (BFS) instead of Q-Learning
    baseline = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.eps = float(self.eps)
        self.balancing_temperature = float(self.balancing_temperature)
        self.gamma = float(self.gamma)
        self.use_urls = bool(int(self.use_urls))
        self.use_pages = int(self.use_pages)
        self.use_same_domain = int(self.use_same_domain)
        self.double = int(self.double)
        self.stay_in_domain = bool(int(self.stay_in_domain))
        self.steps_before_switch = int(self.steps_before_switch)
        self.replay_sample_size = int(self.replay_sample_size)
        self.baseline = bool(int(self.baseline))
        self.Q = QLearner(
            steps_before_switch=self.steps_before_switch,
            replay_sample_size=self.replay_sample_size,
            gamma=self.gamma,
            double_learning=bool(self.double),
            on_model_changed=self.on_model_changed,
            pickle_memory=False,
            dummy=self.baseline,
        )
        self.link_vectorizer = LinkVectorizer(
            use_url=bool(self.use_urls),
            use_same_domain=bool(self.use_same_domain),
        )
        self.page_vectorizer = PageVectorizer()
        self.total_reward = 0
        self.model_changes = 0
        self.goal = self.get_goal()

        self.checkpoint_interval = int(self.checkpoint_interval)
        self._save_params_json()

    def _save_params_json(self):
        if self.checkpoint_path:
            params = json.dumps(self.get_params(), indent=4)
            print(params)
            (Path(self.checkpoint_path)/"params.json").write_text(params)

    @abc.abstractmethod
    def get_goal(self) -> BaseGoal:
        """ This method should return a crawl goal object """
        pass

    def is_seed(self, r: Union[scrapy.Request, Response]) -> bool:
        return 'link_vector' not in r.meta

    def update_node(self, response: Response, data: Dict) -> None:
        """ Store extra information in crawl graph node """
        if not hasattr(self, 'G'):
            return
        node = self.G.node[response.meta['node_id']]
        node['t'] = self.Q.t_
        node.update(data)

    def parse(self, response: Response):
        self.increase_response_count()
        self.close_finished_queues()
        self._debug_expected_vs_got(response)
        output, reward = self._parse(response)
        self.log_stats()
        self.maybe_checkpoint()

        stats = self.get_stats_item()
        stats['is_seed'] = self.is_seed(response)
        stats['reward'] = reward
        stats['url'] = response.url
        stats['Q'] = priority_to_score(response.request.priority)
        stats['eps-policy'] = response.request.meta.get('from_random_policy', None)
        yield stats

        yield from output

    @log_time
    def _parse(self, response):
        if self.is_seed(response) and not hasattr(response, 'text'):
            # bad seed
            return [], 0

        as_t = response.meta.get('link_vector')

        if not hasattr(response, 'text'):
            # learn to avoid non-html responses
            self.Q.add_experience(
                as_t=as_t,
                AS_t1=None,
                r_t1=0
            )
            self.update_node(response, {'reward': 0})
            return [], 0

        page_vector = self._get_page_vector(response)
        links = self._extract_links(response)
        links_matrix = self.link_vectorizer.transform(links) if links else None
        links_matrix = self.Q.join_As(links_matrix, page_vector)

        reward = 0
        if not self.is_seed(response):
            reward = self.goal.get_reward(response)
            self.update_node(response, {'reward': reward})
            self.total_reward += reward
            self.Q.add_experience(
                as_t=as_t,
                AS_t1=links_matrix,
                r_t1=reward
            )
            self.goal.response_observed(response)
        return list(self._links_to_requests(links, links_matrix)), reward

    def _extract_links(self, response: TextResponse) -> List[Dict]:
        """ Return a list of all unique links on a page """
        return list(self.le.iter_link_dicts(
            response=response,
            limit_by_domain=self.stay_in_domain,
            deduplicate=False,
            deduplicate_local=True,
        ))

    def _links_to_requests(self,
                           links: List[Dict],
                           links_matrix: sp.csr_matrix,
                           ) -> Iterator[scrapy.Request]:
        indices_and_links = list(self.le.deduplicate_links_enumerated(links))
        if not indices_and_links:
            return
        indices, links_to_follow = zip(*indices_and_links)
        AS = links_matrix[list(indices)]
        scores = self.Q.predict(AS)

        for link, v, score in zip(links_to_follow, AS, scores):
            url = link['url']
            next_domain = get_domain(url)
            meta = {
                'link_vector': v,
                # 'link': link,  # turn it on for debugging
                'scheduler_slot': next_domain,
            }
            priority = score_to_priority(score)
            req = scrapy.Request(url, priority=priority, meta=meta)
            set_request_domain(req, next_domain)
            yield req

    def _get_page_vector(self, response: TextResponse) -> Optional[np.ndarray]:
        """ Convert response content to a feature vector """
        if not self.use_pages:
            return None
        return self.page_vectorizer.transform([response.text])[0]

    def get_scheduler_queue(self):
        """
        This method is called by deepdeep.scheduler.Scheduler
        to create a new queue.
        """
        def new_queue(domain):
            return RequestsPriorityQueue(fifo=True)
        return BalancedPriorityQueue(
            queue_factory=new_queue,
            eps=self.eps,
            balancing_temperature=self.balancing_temperature,
        )

    @property
    def scheduler(self) -> Scheduler:
        return self.crawler.engine.slot.scheduler

    def on_model_changed(self):
        self.model_changes += 1
        if (self.model_changes % 1) == 0:
            self.recalculate_request_priorities()

    def close_finished_queues(self):
        for slot in self.scheduler.queue.get_active_slots():
            if self.goal.is_acheived_for(domain=slot):
                self.scheduler.close_slot(slot)

    @log_time
    def recalculate_request_priorities(self):
        if self.baseline:
            return

        def request_priorities(requests: List[scrapy.Request]) -> List[int]:
            priorities = np.ndarray(len(requests), dtype=int)
            vectors, indices = [], []
            for idx, request in enumerate(requests):
                if self.is_seed(request):
                    priorities[idx] = request.priority
                    continue
                vectors.append(request.meta['link_vector'])
                indices.append(idx)
            if vectors:
                scores = self.Q.predict(sp.vstack(vectors))
                priorities[indices] = scores * FLOAT_PRIORITY_MULTIPLIER

            # convert priorities to Python ints because scrapy.Request
            # doesn't support numpy int types
            priorities = [p.item() for p in priorities]

            # TODO: use _log_promising_link or remove it
            return priorities

        for slot in tqdm.tqdm(self.scheduler.queue.get_active_slots()):
            queue = self.scheduler.queue.get_queue(slot)
            queue.update_all_priorities(request_priorities)

    def _log_promising_link(self, link, score):
        self.logger.debug("PROMISING LINK {:0.4f}: {}\n        {}".format(
            score, link['url'], link['inside_text']
        ))

    def _examples(self):
        return None, None

    def log_stats(self):
        if self.checkpoint_path:
            print(self.checkpoint_path)
        examples, AS = self._examples()
        if examples:
            scores_target = self.Q.predict(AS)
            scores_online = self.Q.predict(AS, online=True)
            for ex, score1, score2 in zip(examples, scores_target, scores_online):
                print(" {:0.4f} {:0.4f} {}".format(score1, score2, ex))

        print("t={}, return={:0.4f}, avg return={:0.4f}, L2 norm: {:0.4f} {:0.4f}".format(
            self.Q.t_,
            self.total_reward,
            self.total_reward / self.Q.t_ if self.Q.t_ else 0,
            self.Q.coef_norm(online=True),
            self.Q.coef_norm(online=False),
        ))
        self.goal.debug_print()

        stats = self.get_stats_item()
        print("Domains: {domains_open} open, {domains_closed} closed; "
              "{todo} requests in queue, {processed} processed, {dropped} dropped".format(**stats))

    def get_stats_item(self):
        domains_open, domains_closed = self._domain_stats()
        stats = self.crawler.stats  # type: StatsCollector
        enqueued = stats.get_value('custom-scheduler/enqueued/', 0)
        dequeued = stats.get_value('custom-scheduler/dequeued/', 0)
        dropped = stats.get_value('custom-scheduler/dropped/', 0)
        todo = enqueued - dequeued - dropped

        return {
            '_type': 'stats',
            't': self.Q.t_,
            'return': self.total_reward,
            'domains_open': domains_open,
            'domains_closed': domains_closed,
            'enqueued': enqueued,
            'processed': dequeued,
            'dropped': dropped,
            'todo': todo,
        }

    def _debug_expected_vs_got(self, response: Response):
        if 'link' not in response.meta:
            return
        reward = self.goal.get_reward(response)
        self.logger.debug("\nGOT {:0.4f} (expected return was {:0.4f}) {}\n{}".format(
            reward,
            priority_to_score(response.request.priority),
            response.url,
            response.meta['link'].get('inside_text'),
        ))

    def _domain_stats(self) -> Tuple[int, int]:
        domains_open = len(self.scheduler.queue.get_active_slots())
        domains_closed = len(self.scheduler.queue.closed_slots)
        return domains_open, domains_closed

    def get_params(self):
        keys = self._ARGS - {'checkpoint_path', 'checkpoint_interval'}
        params = {key: getattr(self, key) for key in keys}
        if getattr(self, 'crawler', None):
            params['DEPTH_PRIORITY'] = self.crawler.settings.get('DEPTH_PRIORITY')
        return params

    def maybe_checkpoint(self):
        if (self.Q.t_ % self.checkpoint_interval) != 0 or self.Q.t_ == 0:
            return
        self.do_checkpoint()

    def do_checkpoint(self):
        if not self.checkpoint_path:
            return
        path = Path(self.checkpoint_path)
        self.dump_policy(path/("Q-%s.joblib" % self.Q.t_))
        self.dump_crawl_graph(path/"graph.pickle")

    @log_time
    def dump_crawl_graph(self, path):
        if hasattr(self, 'G'):
            nx.write_gpickle(self.G, str(path))

    @log_time
    def dump_policy(self, path):
        """ Save the current policy """
        data = {
            'Q': self.Q,
            'link_vectorizer': self.link_vectorizer,
            'page_vectorizer': self.page_vectorizer,
            '_params': self.get_params(),
        }
        joblib.dump(data, str(path), compress=3)
        self._save_params_json()

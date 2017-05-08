import networkx as nx
import numpy as np

from thespian.actors import *

from router_mixins import RLAgent, LinkStateHolder
from messages import *
from time_actor import *
from utils import mk_current_neural_state, get_data_cols

class RouterNotInitialized(Exception):
    """
    This is raised then some message which is not `RouterInitMsg` comes first to router
    """

class Router(TimeActor):
    def __init__(self):
        super().__init__()
        self.overlord = None
        self.addr = None
        self.network = {}
        self.network_inv = {}
        self.neighbors = {}
        self.pkg_process_delay = 0
        self.queue_time = 0
        self.link_states = {}
        self.full_log = False

    def initialize(self, message, sender):
        super().initialize(message, sender)
        if isinstance(message, RouterInitMsg):
            self.overlord = sender
            self.addr = message.network_addr
            self.neighbors = message.neighbors
            self.network = message.network
            self.pkg_process_delay = message.pkg_process_delay
            self.network_inv = {str(target) : addr for addr, target in message.network.items()}
            self.full_log = message.full_log
            for n in self.neighbors.keys():
                self.link_states[n] = {'transfer_time': 0, 'alive': True}

    def processEvent(self, event):
        if not self.isInitialized():
            raise RouterNotInitialized("Router has not been initialized!")

        if isinstance(event, IncomingPkgEvent):
            self.queue_time = max(self.current_time, self.queue_time) + self.pkg_process_delay
            new_event = ProcessPkgEvent(self.queue_time, event.sender, event.getContents())
            self.event_queue.push(new_event)
        elif isinstance(event, ProcessPkgEvent):
            self.receivePackage(event)
            pkg = event.getContents()
            if self.full_log:
                pkg.route_add(self._currentStateData(pkg), self._currentStateCols())
            else:
                pkg.route_add([self.current_time, self.addr], ['time', 'cur_node'])
            print("ROUTER #{} ROUTES PACKAGE TO {}".format(self.addr, pkg.dst))
            if pkg.dst == self.addr:
                self.reportPkgDone(pkg, self.current_time)
            else:
                best_neighbor = self.routePackage(pkg)
                is_alive = self.link_states[best_neighbor]['alive']
                if is_alive:
                    target = self.network[best_neighbor]
                    link_latency = self.neighbors[best_neighbor]['latency']
                    link_bandwidth = self.neighbors[best_neighbor]['bandwidth']
                    transfer_start_time = max(self.current_time, self.link_states[best_neighbor]['transfer_time'])
                    transfer_end_time = transfer_start_time + (pkg.size / link_bandwidth)
                    finish_time = transfer_end_time + link_latency
                    self.link_states[best_neighbor]['transfer_time'] = transfer_end_time
                    self.sendEvent(target, IncomingPkgEvent(finish_time, self.myAddress, pkg))
                else:
                    self.sendToBrokenLink(pkg)
        elif isinstance(event, LinkBreakMsg):
            n = event.neighbor
            self.link_states[n]['alive'] = False
            self.breakLink(n)
        elif isinstance(event, LinkRestoreMsg):
            n = event.neighbor
            self.link_states[n]['alive'] = True
            self.restoreLink(n)
        else:
            pass

    def isInitialized(self):
        return self.overlord is not None

    def reportPkgDone(self, pkg, time):
        self.send(self.overlord, PkgDoneMsg(time, self.myAddress, pkg))

    def receivePackage(self, pkg_event):
        pass

    def routePackage(self, pkg_event):
        pass

    def sendToBrokenLink(self, pkg):
        pass

    def breakLink(self, v):
        pass

    def restoreLink(self, v):
        pass

    def _nodesList(self):
        return sorted(list(self.network.keys()))

    def _currentStateData(self, pkg):
        pass

    def _currentStateCols(self):
        pass

class LinkStateRouter(Router, LinkStateHolder):
    def __init__(self):
        LinkStateHolder.__init__(self)
        super().__init__()

    def initialize(self, message, sender):
        super().initialize(message, sender)
        if isinstance(message, LinkStateInitMsg):
            self.initGraph(self.addr, self.network, self.neighbors, self.link_states)
            self._announceLinkState()
            self._cur_state_cols = get_data_cols(len(self.network))

    def _announceLinkState(self):
        announcement = self.mkLSAnnouncement(self.addr)
        self._broadcastAnnouncement(announcement, self.myAddress)
        self.seq_num += 1

    def _broadcastAnnouncement(self, announcement, sender):
        for n in self.neighbors.keys():
            if sender != self.network[n]:
                self.sendServiceMsg(self.network[n], announcement)

    def receiveServiceMsg(self, message, sender):
        super().receiveServiceMsg(message, sender)
        if isinstance(message, LinkStateAnnouncement):
            if self.processLSAnnouncement(message, self.network.keys()):
                self._broadcastAnnouncement(message, sender)

    def breakLink(self, v):
        self.lsBreakLink(self.addr, v)
        self._announceLinkState()

    def restoreLink(self, v):
        self.lsRestoreLink(self.addr, v)
        self._announceLinkState()

    def isInitialized(self):
        return super().isInitialized() and (len(self.announcements) == len(self.network))

    def routePackage(self, pkg):
        d = pkg.dst
        path = nx.dijkstra_path(self.network_graph, self.addr, d)
        return path[1]

    def _currentStateData(self, pkg):
        return mk_current_neural_state(self.network_graph, self.current_time, pkg, self.addr)

    def _currentStateCols(self):
        return self._cur_state_cols

class QRouter(Router, RLAgent):
    def __init__(self):
        super().__init__()
        self.reward_pending = {}

    def receivePackage(self, pkg_event):
        self.sendServiceMsg(pkg_event.sender, self.mkRewardMsg(pkg_event.getContents()))

    def routePackage(self, pkg):
        state = self.getState(pkg)
        self.reward_pending[pkg.id] = state
        return self.act(state)

    def receiveServiceMsg(self, message, sender):
        super().receiveServiceMsg(message, sender)
        if isinstance(message, RewardMsg):
            prev_state = 0
            try:
                prev_state = self.reward_pending[message.pkg_id]
            except KeyError:
                print("Unexpected reward msg!")
            self.observe(self.mkSample(message, prev_state, sender))

    def mkRewardMsg(self, pkg):
        pass

    def getState(self, pkg):
        pass

    def mkSample(self, message, prev_state, sender):
        pass

MAX_EPSILON = 1
MIN_EPSILON = 0.01
LAMBDA = 0.001

class SimpleQRouter(QRouter):
    def __init__(self):
        super().__init__()
        self.Q = {}
        self.learning_rate = None
        self.broken_link_Qs = {}
        self.steps = 0

    def initialize(self, message, sender):
        super().initialize(message, sender)
        if isinstance(message, SimpleQRouterInitMsg):
            self.learning_rate = message.learning_rate
            for n in self.network.keys():
                self.Q[n] = {}
                for (k, data) in self.neighbors.items():
                    if k == n:
                        self.Q[n][k] = 40
                    else:
                        self.Q[n][k] = 100500

    def breakLink(self, v):
        broken_Qs = {}
        for n in self.network.keys():
            broken_Qs[n] = self.Q[n][v]
            self.Q[n][v] = 100500
        self.broken_link_Qs[v] = broken_Qs

    def restoreLink(self, v):
        broken_Qs = self.broken_link_Qs[v]
        for (n, val) in broken_Qs.items():
            self.Q[n][v] = val
        del self.broken_link_Qs[v]

    def mkRewardMsg(self, pkg):
        d = pkg.dst
        best_estimate = 0 if self.addr == d else dict_min(self.Q[d])[1]
        return SimpleRewardMsg(pkg.id, self.current_time, best_estimate, d)

    def mkSample(self, message, prev_state, sender):
        if isinstance(message, SimpleRewardMsg):
            sender_addr = self.network_inv[str(sender)]
            sent_time = prev_state[0]
            new_estimate = message.estimate + (message.cur_time - sent_time)
            return (message.dst, sender_addr, new_estimate)
        else:
            raise Exception("Unsupported type of reward msg!")

    def getState(self, pkg):
        return (self.current_time, pkg.dst)

    def act(self, state):
        d = state[1]
        return dict_min(self.Q[d])[0]

    def observe(self, sample):
        (dst, sender_addr, new_estimate) = sample
        delta = self.learning_rate * (new_estimate - self.Q[dst][sender_addr])
        self.Q[dst][sender_addr] += delta

    def _currentStateData(self, pkg):
        return [self.current_time, pkg.id]

    def _currentStateCols(self):
        return ['time', 'pkg_id']

def dict_min(dct):
    return min(dct.items(), key=lambda x:x[1])

def mk_num_list(s, n):
    return list(map(lambda k: s+str(k), range(0, n)))

def mk_unary_arr(n, *pts):
    res = np.zeros(n)
    for p in pts:
        res[p] = 1
    return res

import json
import logging
import os
import threading
import time

from patroni.dcs import AbstractDCS, ClusterConfig, Cluster, Failover, Leader, Member, SyncState, TimelineHistory
from pysyncobj import SyncObj, SyncObjConf, replicated, FAIL_REASON
from pysyncobj.poller import createPoller
from pysyncobj.node import Node, NODE_STATUS


logger = logging.getLogger(__name__)


class SyncObjUtility(object):

    def __init__(self, syncObj):
        self.syncObj = syncObj
        self._poller = createPoller('auto')
        self.__result = None

    def setPartnerAddress(self, partner):
        self.__node = Node(self, partner, True)

    def sendMessage(self, message):
        self.__message = message
        self.__node.connectIfRequired()
        while self.__node.getStatus() != NODE_STATUS.DISCONNECTED:
            self._poller.poll(0.5)
        return self.__result

    # selfAddress is send as a first message, we will abuse that fact
    def _getSelfNodeAddr(self):
        return self.__message

    def _onMessageReceived(self, _, message):
        self.__result = message
        self.__node._Node__conn.disconnect()
        self.__node._Node__lastConnectAttemptTime = 0

    def __getattr__(self, name):
        return getattr(self.syncObj, name)


class DynMemberSyncObj(SyncObj):

    def __init__(self, selfAddress, partnerAddrs, conf):
        autoTick = conf.autoTick
        conf.autoTick = False
        super(DynMemberSyncObj, self).__init__(None, partnerAddrs, conf)

        utility = SyncObjUtility(self)

        add_self = False
        nodes = partnerAddrs[:]
        nodes.extend([n.getAddress() for n in self._SyncObj__nodes if n.getAddress() not in partnerAddrs])
        for node in nodes:
            utility.setPartnerAddress(node)
            response = utility.sendMessage(['members'])
            if response:
                partnerAddrs = [member['addr'] for member in response if member['addr'] != selfAddress]
                if len(partnerAddrs) == len(response):
                    if not conf.dynamicMembershipChange:
                        selfAddress = None
                    elif selfAddress:
                        add_self = True

        conf.autoTick = autoTick
        super(DynMemberSyncObj, self).__init__(selfAddress, partnerAddrs, conf)
        if add_self:
            utility.sendMessage(['add', selfAddress])

    def __get_members(self):
        ret = [{'addr': n.getAddress(), 'status': n.getStatus(),
                'leader': n.getAddress() == self._getLeader()} for n in self._SyncObj__nodes]
        ret.append({'addr': self._getSelfNodeAddr(), 'status': NODE_STATUS.CONNECTED, 'leader': self._isLeader()})
        return ret

    # original __onUtilityMessage returns data in a strange format...
    def _SyncObj__onUtilityMessage(self, conn, message):
        if message[0] == 'members':
            conn.send(self.__get_members())
            return True
        return super(DynMemberSyncObj, self)._SyncObj__onUtilityMessage(conn, message)

    def _SyncObj__doChangeCluster(self, request, reverse=False):
        ret = super(DynMemberSyncObj, self)._SyncObj__doChangeCluster(request, reverse)
        if ret:
            self.forceLogCompaction()
        return ret


class KVStoreTTL(DynMemberSyncObj):

    def __init__(self, selfAddress, partnerAddrs, conf, on_set=None, on_delete=None):
        self.__on_set = on_set
        self.__on_delete = on_delete
        self.__limb = {}
        self.__retry_timeout = None
        self.__early_apply_local_log = selfAddress is not None
        self.applied_local_log = False
        super(KVStoreTTL, self).__init__(selfAddress, partnerAddrs, conf)
        self.__data = {}

    @staticmethod
    def __check_requirements(old_value, **kwargs):
        return ('prevExist' not in kwargs or bool(kwargs['prevExist']) == bool(old_value)) and \
            ('prevValue' not in kwargs or old_value and old_value['value'] == kwargs['prevValue']) and \
            (not kwargs.get('prevIndex') or old_value and old_value['index'] == kwargs['prevIndex'])

    def set_retry_timeout(self, retry_timeout):
        self.__retry_timeout = retry_timeout

    def retry(self, func, *args, **kwargs):
        event = threading.Event()
        ret = {'result': None, 'error': -1}

        def callback(result, error):
            ret.update(result=result, error=error)
            event.set()

        kwargs['callback'] = callback
        timeout = kwargs.pop('timeout', None) or self.__retry_timeout
        deadline = timeout and time.time() + timeout

        while True:
            event.clear()
            func(*args, **kwargs)
            event.wait(timeout)
            if ret['error'] == FAIL_REASON.SUCCESS:
                return ret['result']
            elif ret['error'] == FAIL_REASON.REQUEST_DENIED:
                break
            elif deadline:
                timeout = deadline - time.time()
                if timeout <= 0:
                    break
            time.sleep(1)
        return False

    @replicated
    def _set(self, key, value, **kwargs):
        old_value = self.__data.get(key, {})
        if not self.__check_requirements(old_value, **kwargs):
            return False

        if old_value and old_value['created'] != value['created']:
            value['created'] = value['updated']
        value['index'] = self._SyncObj__raftLastApplied + 1

        self.__data[key] = value
        if self.__on_set:
            self.__on_set(key, value)
        return True

    def set(self, key, value, ttl=None, **kwargs):
        old_value = self.__data.get(key, {})
        if not self.__check_requirements(old_value, **kwargs):
            return False

        value = {'value': value, 'updated': time.time()}
        value['created'] = old_value.get('created', value['updated'])
        if ttl:
            value['expire'] = value['updated'] + ttl
        return self.retry(self._set, key, value, **kwargs)

    def __pop(self, key):
        self.__data.pop(key)
        if self.__on_delete:
            self.__on_delete(key)

    @replicated
    def _delete(self, key, recursive=False, **kwargs):
        if recursive:
            for k in list(self.__data.keys()):
                if k.startswith(key):
                    self.__pop(k)
        elif not self.__check_requirements(self.__data.get(key, {}), **kwargs):
            return False
        else:
            self.__pop(key)
        return True

    def delete(self, key, recursive=False, **kwargs):
        if not recursive and not self.__check_requirements(self.__data.get(key, {}), **kwargs):
            return False
        return self.retry(self._delete, key, recursive=recursive, **kwargs)

    @staticmethod
    def __values_match(old, new):
        return all(old.get(n) == new.get(n) for n in ('created', 'updated', 'expire', 'value'))

    @replicated
    def _expire(self, key, value, callback=None):
        current = self.__data.get(key)
        if current and self.__values_match(current, value):
            self.__pop(key)

    def __expire_keys(self):
        for key, value in self.__data.items():
            if value and 'expire' in value and value['expire'] <= time.time() and \
                    not (key in self.__limb and self.__values_match(self.__limb[key], value)):
                self.__limb[key] = value

                def callback(*args):
                    if key in self.__limb and self.__values_match(self.__limb[key], value):
                        self.__limb.pop(key)
                self._expire(key, value, callback=callback)

    def get(self, key, recursive=False):
        if not recursive:
            return self.__data.get(key)
        return {k: v for k, v in self.__data.items() if k.startswith(key)}

    def _onTick(self, timeToWait=0.0):
        # The SyncObj starts applying the local log only when there is at least one node connected.
        # We want to change this behavior and apply the local log even when there is nobody except us.
        # It gives us at least some picture about the last known cluster state.
        if self.__early_apply_local_log and not self.applied_local_log and self._SyncObj__needLoadDumpFile:
            self._SyncObj__raftCommitIndex = self._SyncObj__getCurrentLogIndex()
            self._SyncObj__raftCurrentTerm = self._SyncObj__getCurrentLogTerm()

        super(KVStoreTTL, self)._onTick(timeToWait)

        # The SyncObj calls onReady callback only when cluster got the leader and is ready for writes.
        # In some cases for us it is safe to "signal" the Raft object when the local log is fully applied.
        # We are using the `applied_local_log` property for that, but not calling the callback function.
        if self.__early_apply_local_log and not self.applied_local_log and self._SyncObj__raftCommitIndex != 1 and \
                self._SyncObj__raftLastApplied == self._SyncObj__raftCommitIndex:
            self.applied_local_log = True

        if self._isLeader():
            self.__expire_keys()
        else:
            self.__limb.clear()


class Raft(AbstractDCS):

    def __init__(self, config):
        super(Raft, self).__init__(config)
        self._ttl = int(config.get('ttl') or 30)

        self_addr = None if self._ctl else config.get('self_addr')
        template = os.path.join(config.get('data_dir', ''), self_addr or '')
        files = {'journalFile': template + '.journal', 'fullDumpFile': template + '.dump'} if self_addr else {}

        ready_event = threading.Event()
        conf = SyncObjConf(commandsWaitLeader=False, appendEntriesUseBatch=False, onReady=ready_event.set,
                           dynamicMembershipChange=True, **files)
        self._sync_obj = KVStoreTTL(self_addr, config.get('partner_addrs', []), conf, self._on_set, self._on_delete)
        while True:
            ready_event.wait(5)
            if ready_event.isSet() or self._sync_obj.applied_local_log:
                break
            else:
                logger.info('waiting on raft')
        self._sync_obj.forceLogCompaction()
        self.set_retry_timeout(int(config.get('retry_timeout') or 10))

    def _on_set(self, key, value):
        if value['created'] == value['updated'] and (key.startswith(self.members_path) or key == self.leader_path) \
                or key in (self.config_path, self.sync_path):
            self.event.set()

    def _on_delete(self, key):
        if key == self.leader_path:
            self.event.set()

    def set_ttl(self, ttl):
        self._ttl = ttl

    @property
    def ttl(self):
        return self._ttl

    def set_retry_timeout(self, retry_timeout):
        self._sync_obj.set_retry_timeout(retry_timeout)

    @staticmethod
    def member(key, value):
        return Member.from_node(value['index'], os.path.basename(key), None, value['value'])

    def _load_cluster(self):
        prefix = self.client_path('')
        response = self._sync_obj.get(prefix, recursive=True)
        if not response:
            return Cluster(None, None, None, None, [], None, None, None)
        nodes = {os.path.relpath(key, prefix).replace('\\', '/'): value for key, value in response.items()}

        # get initialize flag
        initialize = nodes.get(self._INITIALIZE)
        initialize = initialize and initialize['value']

        # get global dynamic configuration
        config = nodes.get(self._CONFIG)
        config = config and ClusterConfig.from_node(config['index'], config['value'])

        # get timeline history
        history = nodes.get(self._HISTORY)
        history = history and TimelineHistory.from_node(history['index'], history['value'])

        # get last leader operation
        last_leader_operation = nodes.get(self._LEADER_OPTIME)
        last_leader_operation = 0 if last_leader_operation is None else int(last_leader_operation['value'])

        # get list of members
        members = [self.member(k, n) for k, n in nodes.items() if k.startswith(self._MEMBERS) and k.count('/') == 1]

        # get leader
        leader = nodes.get(self._LEADER)
        if leader:
            member = Member(-1, leader['value'], None, {})
            member = ([m for m in members if m.name == leader['value']] or [member])[0]
            leader = Leader(leader['index'], None, member)

        # failover key
        failover = nodes.get(self._FAILOVER)
        if failover:
            failover = Failover.from_node(failover['index'], failover['value'])

        # get synchronization state
        sync = nodes.get(self._SYNC)
        sync = SyncState.from_node(sync and sync['index'], sync and sync['value'])

        return Cluster(initialize, config, leader, last_leader_operation, members, failover, sync, history)

    def _write_leader_optime(self, last_operation):
        return self._sync_obj.set(self.leader_optime_path, last_operation, timeout=1)

    def _update_leader(self):
        ret = self._sync_obj.set(self.leader_path, self._name, ttl=self._ttl, prevValue=self._name)
        if not ret and self._sync_obj.get(self.leader_path) is None:
            ret = self.attempt_to_acquire_leader()
        return ret

    def attempt_to_acquire_leader(self, permanent=False):
        return self._sync_obj.set(self.leader_path, self._name, prevExist=False,
                                  ttl=None if permanent else self._ttl)

    def set_failover_value(self, value, index=None):
        return self._sync_obj.set(self.failover_path, value, prevIndex=index)

    def set_config_value(self, value, index=None):
        return self._sync_obj.set(self.config_path, value, prevIndex=index)

    def touch_member(self, data, permanent=False):
        data = json.dumps(data, separators=(',', ':'))
        return self._sync_obj.set(self.member_path, data, None if permanent else self._ttl, timeout=2)

    def take_leader(self):
        return self._sync_obj.set(self.leader_path, self._name, ttl=self._ttl)

    def initialize(self, create_new=True, sysid=''):
        return self._sync_obj.set(self.initialize_path, sysid, prevExist=(not create_new))

    def delete_leader(self):
        return self._sync_obj.delete(self.leader_path, prevValue=self._name, timeout=1)

    def cancel_initialization(self):
        return self._sync_obj.delete(self.initialize_path)

    def delete_cluster(self):
        return self._sync_obj.delete(self.client_path(''), recursive=True)

    def set_history_value(self, value):
        return self._sync_obj.set(self.history_path, value)

    def set_sync_state_value(self, value, index=None):
        return self._sync_obj.set(self.sync_path, value, prevIndex=index)

    def delete_sync_state(self, index=None):
        return self._sync_obj.delete(self.sync_path, prevIndex=index)

    def watch(self, leader_index, timeout):
        try:
            return super(Raft, self).watch(leader_index, timeout)
        finally:
            self.event.clear()
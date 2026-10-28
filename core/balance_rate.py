"""kt00018-only, per app/server budget shared by threads and local processes.

No token, account, balance or order data is stored. The dedicated lock is held
through HTTP completion; its quiet interval starts AFTER completion, so paused
threads cannot bunch previously reserved slots. Other TRs never take this lock.
"""
from contextlib import contextmanager
import hashlib,json,math,os,threading,time
from pathlib import Path
from core.util import ProcessLock,atomic_json

_locks={}
_registry_lock=threading.Lock()


class BalanceBudgetError(RuntimeError):
    pass


class BalanceRateBudget:
    def __init__(self,identity,*,mock=False,root=None,monotonic=None,wall=None,sleep=None,max_wait=3.):
        self.gap=1.05 if mock else .55
        self.monotonic=monotonic or time.monotonic;self.wall=wall or time.time;self.sleep=sleep or time.sleep
        self.max_wait=max_wait
        base=Path(root) if root is not None else Path(os.environ.get('LOCALAPPDATA',Path.home()/'.cache'))/'StockTradingBot'/'tr-budget'
        # Hash again: the path never contains a credential or arbitrary input.
        self.path=base/(hashlib.sha256(identity.encode()).hexdigest()+'.kt00018.json')
        self.lock_path=self.path.with_suffix('.lock')
        with _registry_lock:self.thread_lock=_locks.setdefault(str(self.path.absolute()),threading.Lock())

    @contextmanager
    def locked(self,diagnostic=False):
        began=self.monotonic();timeout=0 if diagnostic else self.max_wait
        if not self.thread_lock.acquire(timeout=timeout):raise BalanceBudgetError('KT00018_BUDGET_BUSY')
        try:
            while True:
                lock=ProcessLock(self.lock_path)
                try:lock.__enter__();break
                except OSError as error:raise BalanceBudgetError('KT00018_BUDGET_STORAGE_UNAVAILABLE') from error
                except RuntimeError:
                    if diagnostic or self.monotonic()-began>=timeout:raise BalanceBudgetError('KT00018_BUDGET_BUSY') from None
                    self.sleep(.02)
            try:yield began
            finally:lock.__exit__(None,None,None)
        finally:self.thread_lock.release()

    def read(self):
        try:
            with self.path.open('r',encoding='utf-8') as stream:state=json.load(stream)
        except FileNotFoundError:return None
        except OSError as error:raise BalanceBudgetError('KT00018_BUDGET_STORAGE_UNAVAILABLE') from error
        except (ValueError,UnicodeError) as error:raise BalanceBudgetError('KT00018_BUDGET_STATE_INVALID') from error
        if (not isinstance(state,dict) or state.get('version')!=1 or type(state.get('inflight')) is not bool
                or any(type(state.get(k)) not in (int,float) or not math.isfinite(state[k])
                       for k in ('wall','mono','ready_wall','ready_mono'))):
            raise BalanceBudgetError('KT00018_BUDGET_STATE_INVALID')
        return state

    def write(self,state):
        # This is a throttle checkpoint, not a financial ledger. Atomic replace
        # prevents partial reads. An unfinished/crashed request costs a full
        # quiet second on recovery. Invalid state fails closed.
        try:atomic_json(self.path,state,fsync=False)
        except OSError as error:raise BalanceBudgetError('KT00018_BUDGET_STORAGE_UNAVAILABLE') from error

    def state(self,inflight=False,delay=None):
        wall=self.wall();mono=self.monotonic();delay=self.gap if delay is None else delay
        return dict(version=1,inflight=inflight,wall=wall,mono=mono,
                    ready_wall=wall+delay,ready_mono=mono+delay)

    def remaining(self,state):
        if state is None:return 0.
        if state['inflight']:return max(1.05,self.gap)
        wall=self.wall();mono=self.monotonic()
        if abs((wall-mono)-(state['wall']-state['mono']))>2:
            # Reboot/clock adjustment: never interpret a negative timestamp as
            # instant permission. Re-establish a quiet interval under the lock.
            return max(1.05,min(3600.,max(0.,state['ready_wall']-wall)))
        return max(0.,state['ready_wall']-wall,state['ready_mono']-mono)

    @contextmanager
    def slot(self,diagnostic=False):
        with self.locked(diagnostic) as began:
            remaining=self.remaining(self.read())
            if remaining and (diagnostic or remaining>max(0,self.max_wait-(self.monotonic()-began))):
                raise BalanceBudgetError('KT00018_BUDGET_COOLDOWN')
            if remaining:self.sleep(remaining)
            # Do not catch HTTP/transport exceptions raised by the caller.
            self.write(self.state(inflight=True))
            try:yield (self.monotonic()-began)*1000
            finally:self.write(self.state())

    def backoff(self,seconds):
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds<0:
            raise BalanceBudgetError('KT00018_BACKOFF_INVALID')
        try:
            with self.locked():
                delay=max(seconds,self.remaining(self.read()))
                self.write(self.state(delay=delay))
        except OSError as error:raise BalanceBudgetError('KT00018_BUDGET_STORAGE_UNAVAILABLE') from error

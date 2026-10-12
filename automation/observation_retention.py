"""Bounded subscription continuity; retained roles never gain BUY permission."""

class ObservationRetention:
    def __init__(self):self.last_watch={}

    def select(self,watch,critical,shadow,stamp,grace_seconds,capacity):
        watch=set(watch);critical=set(critical)|watch
        # The observer already ranked its symbols. Preserve that order when
        # bounded retention competes with the diagnostic subscription slots.
        shadow_order=list(dict.fromkeys(shadow));shadow=set(shadow_order)
        self.last_watch={s:t for s,t in self.last_watch.items()
            if t.date()==stamp.date() and 0<=(stamp-t).total_seconds()<=grace_seconds}
        self.last_watch.update({s:stamp for s in watch})
        retained=sorted(set(self.last_watch)-critical,key=lambda s:(-self.last_watch[s].timestamp(),s))
        # Critical positions/orders/external protection are never sacrificed.
        # Use the configured observation budget. If the required existing
        # critical + shadow footprint exceeds it, grace may replace diagnostics
        # but must not expand that footprint further.
        budget=max(capacity,len(critical|shadow))
        selected=set(critical)
        selected.update(retained[:max(0,budget-len(selected))])
        grace=set(selected)-critical
        selected.update([s for s in shadow_order if s not in selected][:max(0,budget-len(selected))])
        return selected,selected-critical,grace

import glob, os, sys, time

pid = sys.argv[1]
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0


def snap():
    d = {}
    for p in glob.glob("/proc/%s/task/*" % pid):
        tid = os.path.basename(p)
        try:
            st = open(p + "/stat").read().split()
            name = open(p + "/comm").read().strip()
            d[tid] = (int(st[13]) + int(st[14]), name, st[2])
        except Exception:
            pass
    return d


a = snap()
time.sleep(secs)
b = snap()
HZ = float(os.sysconf("SC_CLK_TCK"))
rows = []
for tid, (t1, name, state) in b.items():
    t0 = a.get(tid, (t1, name, state))[0]
    rows.append(((t1 - t0) / HZ / secs * 100.0, tid, name, state))
rows.sort(reverse=True)
print("线程数: %d   采样 %.1fs" % (len(rows), secs))
print("%-8s %-8s %-14s %s" % ("CPU%", "tid", "name", "state"))
for pct, tid, name, state in rows[:16]:
    if pct >= 0.5:
        print("%-8.1f %-8s %-14s %s" % (pct, tid, name, state))
print("合计: %.0f%%" % sum(r[0] for r in rows))
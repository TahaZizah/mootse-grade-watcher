# Mootse grade watcher

Sends a push to your phone (via [ntfy](https://ntfy.sh)) as soon as a grade is
added or changed anywhere on Mootse, including the "Élément manuel" grades
that never show up on the overview page and never trigger a Moodle
notification.

Example push:

> **New grade: Génie Logiciel et méthodes agiles**
> TP noté 2: 14,00 / 20
> Course total: 14,00 / 20

## How it works

```
cron-job.org ──every 5 min──▶ GitHub Actions ──▶ mootse_watch.py ──▶ Mootse API
                                                       │
                                     new/changed grade ├──▶ ntfy ──▶ your phone
                                       check succeeded └──▶ healthchecks.io
                                                               │ no success for 30 min
                                                               └──▶ ntfy "watcher down" alert
```

- **cron-job.org** starts a check every 5 minutes. Each check is independent,
  so if GitHub drops one run, the next one still comes. (GitHub's own cron is
  unreliable on this repo, so it's only kept as a backup every 30 minutes.)
- **The script** reads every grade item in every course through the same API
  the Moodle mobile app uses, compares it with the last check, and pushes
  what changed. It retries network errors, only marks a grade as "seen" once
  its push went through (so a failed push is retried, never lost), and never
  forgets a seen grade (so a course that fails to load once can't cause
  duplicate pushes).
- **state.json** holds keyed fingerprints of your grades (HMAC with your
  Mootse token as the key). Nothing in it can be read back without the
  token, so the repo can stay public, which gives it unlimited free
  Actions minutes.
- **healthchecks.io** is told about every successful check. If none succeeds
  for 30 minutes, for any reason (GitHub down, Mootse down, cron-job.org
  stopped, token expired), it sends a "down" alert to your phone, then an
  "up" alert when checks work again.

## Secrets (repo Settings → Secrets and variables → Actions)

| Secret         | What                                                    |
|----------------|---------------------------------------------------------|
| `MOODLE_TOKEN` | Mootse mobile-app token (see below)                     |
| `NTFY_TOPIC`   | The ntfy topic your phone is subscribed to              |
| `HC_PING_URL`  | healthchecks.io ping URL for the "Mootse watcher" check |

## Renewing

### Mootse token expired

You get a "token expired" push. Get a new token by running this in a
terminal, which asks for your Mootse login (the password isn't stored):

```bash
python3 mootse_watch.py --get-token
```

or, without the script:

```bash
bash -c 'read -p "Mootse username: " U; read -s -p "Password: " P; echo; curl -s https://mootse.telecom-st-etienne.fr/login/token.php --data-urlencode "username=$U" --data-urlencode "password=$P" -d service=moodle_mobile_app; echo'
```

Paste the new token into the `MOODLE_TOKEN` secret. The next check
re-baselines quietly (no flood of old grades).

### GitHub key used by cron-job.org (expires 9 Nov 2027)

1. GitHub → Settings → Developer settings → Fine-grained tokens → generate a
   new token: only the `mootse-grade-watcher` repo, permission
   **Actions: Read and write**.
2. cron-job.org → the "Mootse grade watcher" job → Edit → Advanced → header
   `Authorization` = `Bearer <new token>` → Save.

## Running a check by hand

Actions tab → **Mootse grade watcher** → **Run workflow**, or locally:

```bash
MOODLE_TOKEN=… NTFY_TOPIC=… python3 mootse_watch.py
```

Python 3 only, no packages needed.

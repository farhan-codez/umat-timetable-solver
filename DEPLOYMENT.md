# Deploying the Timetable Builder (admin) + publishing to the student app

Two systems talk to each other:

1. **Timetable Builder (admin)** - the FastAPI web app in this repo. Generates
   timetables and publishes them. Runs where the course data lives.
2. **Student app** - the Next.js app (separate repo, `umat-student-app`) hosted
   on Vercel at `https://umat-student-app.vercel.app`. Students and lecturers
   read the published timetable from it.

---

## 1. Host the admin app anywhere

The admin app is a normal web server, so you can put it on any always-on
machine. All data and timetables live in this repo's `data/` and `output/`
folders - move the whole repo with them.

### Option A - Docker (recommended for a server)

```bash
docker build -t umat-timetable-builder .
docker run -d --name timetable-builder -p 8000:8000 \
  -e ADMIN_PASSWORD="a-strong-password" \
  -e STUDENT_APP_URL="https://umat-student-app.vercel.app" \
  -e STUDENT_APP_PUBLISH_SECRET="<shared secret>" \
  -v /path/to/this/repo/data:/app/data \
  -v /path/to/this/repo/output:/app/output \
  umat-timetable-builder
```

- Replace `/path/to/this/repo` with a copy of this repo on the server (the
  `data/` and `output/` folders must contain your semesters and generated
  timetables).
- Then open `http://<server-ip>:8000`. You can reach it from any PC on the
  internet once the port is open.
- On **Railway** or **Render**: connect the repo (or a Dockerfile build),
  set the same environment variables, and they give you a public
  `https://...` URL automatically.
- Keep the Docker image on Python 3.12 (the CP-SAT solver does not support
  newer Python versions yet).

### Option B - No server? Use a tunnel on your PC

If you prefer running it on your own PC, make it reachable from outside with a
free tunnel - e.g. Cloudflare Tunnel (`cloudflared tunnel --url http://localhost:8000`)
or ngrok. Then the URL you get is the admin address you can open from any PC.

---

## 2. Environment variables (admin app)

| Variable | Purpose |
| --- | --- |
| `ADMIN_PASSWORD` | Password for the admin "publish / edit data" actions (always set this when remote). |
| `STUDENT_APP_URL` | Base URL of the student app, e.g. `https://umat-student-app.vercel.app`. If set, the Publish button also pushes the timetable to the student app. |
| `STUDENT_APP_PUBLISH_SECRET` | Shared secret used to authenticate pushes to the student app. Must match `TIMETABLE_PUBLISH_SECRET` there. |

Without `STUDENT_APP_URL` the Publish button still works - it just produces a
shareable link (the old behaviour) instead of pushing to the mobile app.

---

## 3. Student app: publish endpoint (one-time setup on Vercel)

The student app already contains:

- `POST /api/timetable/publish` - receives a published timetable and replaces
  that semester's entries in the database (protected by a secret header).
- `GET /api/timetable` - now serves the admin-published timetable to students
  and lecturers (with the sample/demo data as a fallback when nothing has been
  published yet).

On Vercel, set one environment variable:

- `TIMETABLE_PUBLISH_SECRET` - any long random string. This is the same value
  you put in `STUDENT_APP_PUBLISH_SECRET` on the admin side.

(Also keep `DATABASE_URL`/`DIRECT_URL` as they are - Vercel + the hosted
Postgres are already configured.)

---

## 4. Publishing flow (how it works)

1. Admin clicks **Generate Timetable** (or solves from the web) and reviews it.
2. Admin clicks **Publish**.
3. The admin app writes `output/<semester>/published.json` and:
   - (always) returns a tokenized share link
     `/api/public/timetable?semester=<sem>&token=<token>` for the old flow;
   - (if configured) POSTs the rows to the student app's
     `/api/timetable/publish`, which atomically replaces that semester's
     published entries.
4. Students and lecturers open the mobile app; their timetable pages now show
   the real timetable instead of the sample data.

Everything is idempotent: publishing twice just replaces the semester's data
again. The lecturer page filters by lecturer name automatically; the dashboard
shows the full published week.
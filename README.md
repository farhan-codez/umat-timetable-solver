# UMaT-SRID Timetable Builder

Automatically schedules every lecture/practical into the week for UMaT-SRID using the
OR-Tools CP-SAT solver, then shows it in a website. No clashes, capacities respected,
and it minimises student gaps, evening classes, and wasted room space.

## Starting the website

1. **Double-click `run_web.bat`** in this folder.
2. A black window opens and your browser opens the timetable website automatically.
3. Leave the black window open while the website is in use. To stop, close it.

The first time you run it, it installs the required packages (needs an internet
connection) and can take a few minutes.

If a firewall prompt appears, click **Allow** so other computers on the network can
view the timetable too. Other people can open the site from their browser using this
computer's address: `http://<this-computer-ip>:8000`.

## Admin password

- Viewing the timetable needs **no password**.
- Editing courses/rooms, importing data, and generating a timetable require the
  **admin password**.
- On startup the window prints the admin password. It is saved in the **`security.json`**
  file in this folder — open it in Notepad to read or change it (then restart the app).

## Normal use

- **Timetable tab** — everyone can view the generated timetable by day, cohort,
  lecturer, or room, and download it as an Excel file.
- **Generate Timetable** (after admin login) — builds the timetable. If your data has
  not changed, it reuses the last saved timetable instantly.
- **Courses / Rooms / Cohorts tabs** — the lists used to build the timetable. Edit and
  click "Save changes".

## Importing a new semester

1. Admin login, open the **Import** tab.
2. Select all the department files for the semester (the course-distribution workbooks
   **and** the assembled teaching timetable) and click **Upload files**.
3. Click **Rebuild courses from timetable** — the app reads the teaching timetable and
   rebuilds the Courses and Rooms lists.
4. Check the **Courses**, **Rooms** and **Cohorts** tabs, fix anything unusual, then
   go to the **Timetable** tab and click **Generate Timetable**.

## Where the files live

| Folder / file | What it is |
|---------------|------------|
| `data/courses.xlsx`, `rooms.xlsx`, `cohorts.xlsx` | The data the timetable is built from |
| `data/input/` | Uploaded semester files (the raw source) |
| `output/timetable.xlsx` | The generated timetable (Excel) |
| `output/solve_cache.pkl` | Last good schedule, reused when data has not changed |
| `security.json` | Admin password |

## Troubleshooting

- **"file is open in Excel"** — close that Excel workbook and click Save again.
- **Site won't open on other computers** — make sure the black window is still open and
  the firewall prompt was allowed.
- **The window closes and the site stops** — start it again with `run_web.bat`.
- To make a backup, copy the whole folder.

## Running without the web app

You can also solve from the command line:

```powershell
.\.venv\Scripts\python.exe -m src.main            # solve whatever is in data/
.\.venv\Scripts\python.exe -m src.main --time-limit 120
```

Result: `output/timetable.xlsx`.

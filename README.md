# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: review findings and implementation decisions.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Run on Windows

Use Python 3.11 or newer and MongoDB 6.0 or newer (MongoDB 7 is recommended). Start a local MongoDB instance
in another PowerShell window if needed:

```powershell
docker run --name hrone-mongo -p 27017:27017 -d mongo:7
```

From the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "hrone_assignment"
.\.venv\Scripts\python.exe sample_seed.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

`sample_seed.py` replaces the two collections in the selected database. Use a disposable database when seeding.
For a readiness check, open a second PowerShell window and run:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

The API creates its indexes at startup. You can omit the seed command when using an existing database.

## Run tests

From the repository root, run the discovered standard-library tests with the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

The suite includes unit tests and HTTP integration tests. Integration tests start their own API process and use a
uniquely named disposable MongoDB database; they skip with an explicit reason if MongoDB is unavailable.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
Do not submit `.env` files, secrets, Dockerfiles, virtual environments or database dumps.

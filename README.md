# HROne Attendance & Analytics API

FastAPI service for employee attendance, regularization, and analytics using MongoDB.

## Setup

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Set `MONGO_URI` and `MONGO_DB` as environment variables before starting the application.

API documentation: `http://127.0.0.1:8000/docs`

## Notes

- MongoDB 6.0 or newer is required.
- Indexes are created by the application at startup.
- See `REVIEW.md` for code-review findings and `DECISIONS.md` for design decisions.

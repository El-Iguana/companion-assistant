"""Dev launcher: uvicorn with autoreload on port 7861."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7861, reload=True)

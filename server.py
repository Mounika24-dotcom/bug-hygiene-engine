from fastapi import FastAPI
import uvicorn
import asyncio
from main import run_engine   # your existing pipeline

app = FastAPI()

@app.get("/analyze")
async def analyze(url: str):
    result = await run_engine(url)
    return {
        "score": result["score"],
        "issues": result["issues"],
        "classification": result["classification"],
        "page_type": result["page_type"],
        "graph": result["graph"]
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

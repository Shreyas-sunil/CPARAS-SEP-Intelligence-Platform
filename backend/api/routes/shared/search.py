"""
Semantic Search API
Searches the local vector database for similar patents, specs, and SEP intelligence.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from rag.vector_store import get_vector_store

router = APIRouter()

class SearchQuery(BaseModel):
    """Body of POST /api/search/semantic: query text, number of hits, optional source_type filter (e.g.
    "3gpp_spec", "etsi_ipr", "patent"; None = all) and a minimum similarity score.
    """
    query: str
    top_k: int = 10
    source_type: Optional[str] = None   # "3gpp_spec" | "etsi_ipr" | "patent" | None for all
    min_score: float = 0.5

@router.post("/semantic")
async def semantic_search(body: SearchQuery):
    """Semantic search over the Standards Corpus vector store (rag/vector_store.py)."""
    vs = get_vector_store()
    try:
        results = await vs.similarity_search(
            body.query,
            top_k=body.top_k,
            filter_source=body.source_type,
            min_score=body.min_score,
        )
        return {"query": body.query, "results": results, "total": len(results)}
    except Exception as e:
        raise HTTPException(500, f"Search failed: {e}")

@router.get("/stats")
async def standards_corpus_stats():
    """Standards Corpus vector-store statistics (chunk counts per source type)."""
    vs = get_vector_store()
    try:
        return await vs.get_stats()
    except Exception as e:
        return {"error": str(e), "indexed_chunks": 0}

"""
Test doubles for the embedding path (shared by tests/test_memory_bounded_indexing.py).

FakeModel quacks like a SentenceTransformer for exactly what rag.vector_store.LocalEmbedder uses
(tokenizer, max_seq_length, encode(), device, get_sentence_embedding_dimension()) and SIMULATES the
allocation that crashed the Windows import: an attention-score tensor of
    batch x heads x padded_tokens^2 x 4 bytes
and, when that exceeds ``alloc_limit_bytes``, raises the same error text PyTorch's CPU allocator
raises.  It does not run a network, so it proves the importer's behaviour, not model quality.
"""
from __future__ import annotations

import hashlib
import threading
import time

import numpy as np

VOCAB_WORDS = [f"w{i}" for i in range(200)]


def make_tokenizer():
    """A real fast tokenizer (offsets supported): one token per whitespace-separated word."""
    from tokenizers import Tokenizer, models, pre_tokenizers
    from transformers import PreTrainedTokenizerFast
    vocab = {"[UNK]": 0, "[PAD]": 1}
    vocab.update({w: i + 2 for i, w in enumerate(VOCAB_WORDS)})
    tk = Tokenizer(models.WordLevel(vocab=vocab, unk_token="[UNK]"))
    tk.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    return PreTrainedTokenizerFast(tokenizer_object=tk, unk_token="[UNK]", pad_token="[PAD]")


def torch_alloc_error(nbytes: int) -> RuntimeError:
    return RuntimeError(f"[enforce fail at alloc_cpu.cpp:127] err == 0. DefaultCPUAllocator: can't allocate memory: "
                        f"you tried to allocate {nbytes} bytes. Error code 12 (Cannot allocate memory)")


class FakeModel:
    device = "cpu"

    def __init__(self, *, max_seq_length=8192, heads=16, alloc_limit_bytes=None, oom_if=None, dim=16, delay=0.0):
        self.tokenizer = make_tokenizer()
        self.max_seq_length = max_seq_length
        self.heads = heads
        self.alloc_limit_bytes = alloc_limit_bytes
        self.oom_if = oom_if                  # callable(list_of_texts) -> True to raise a memory error
        self.dim = dim
        self.delay = delay
        self.encode_calls: list[dict] = []    # one entry per ST sub-batch actually "run"
        self.peak_alloc = 0
        self._lock = threading.Lock()
        self.concurrent = 0
        self.max_concurrent = 0

    def get_sentence_embedding_dimension(self):
        return self.dim

    def _tok_len(self, text: str) -> int:
        return len(self.tokenizer(text, add_special_tokens=False, truncation=False, verbose=False)["input_ids"])

    def encode(self, texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True):
        with self._lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.delay:
                time.sleep(self.delay)
            lens = [min(self._tok_len(t), self.max_seq_length) for t in texts]
            order = sorted(range(len(texts)), key=lambda i: -lens[i])      # sentence-transformers sorts longest first
            out = [None] * len(texts)
            for s in range(0, len(order), batch_size):
                idx = order[s:s + batch_size]
                padded = max(lens[i] for i in idx)
                nbytes = len(idx) * self.heads * padded * padded * 4        # the attention-score tensor
                self.peak_alloc = max(self.peak_alloc, nbytes)
                self.encode_calls.append({"batch": len(idx), "padded_tokens": padded, "alloc_bytes": nbytes})
                if self.alloc_limit_bytes is not None and nbytes > self.alloc_limit_bytes:
                    raise torch_alloc_error(nbytes)
                if self.oom_if is not None and self.oom_if([texts[i] for i in idx]):
                    raise torch_alloc_error(nbytes or 1_170_000_000)
                for i in idx:
                    seed = int.from_bytes(hashlib.sha256(texts[i].encode("utf-8")).digest()[:8], "little")
                    v = np.random.default_rng(seed).standard_normal(self.dim).astype("float32")
                    out[i] = v / np.linalg.norm(v)
            return np.asarray(out, dtype="float32")
        finally:
            with self._lock:
                self.concurrent -= 1


def words(n: int, start: int = 0) -> str:
    """n whitespace-separated vocabulary words (n tokens for FakeModel)."""
    return " ".join(VOCAB_WORDS[(start + i) % len(VOCAB_WORDS)] for i in range(n))

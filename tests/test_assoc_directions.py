"""Tests for independent associative search directions and batch scatter."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import numpy as np

from assoc_index import AssocIndex, _literal_matches
from embedding_engine import EmbeddingEngine


class _BatchClient:
    def __init__(self, data):
        self.data = data
        self.calls = []
        self.embeddings = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(data=self.data)


class AssocDirectionTests(unittest.TestCase):
    def test_literal_equality_precedes_containment_and_respects_word_boundaries(self):
        rows = [{"quote": "Support request"}, {"quote": "Port confirmed"}, {"quote": "PORT."}]
        self.assertEqual(_literal_matches("「Ｐｏｒｔ」", rows), [rows[2], rows[1]])

    def test_round_robin_keeps_each_semantic_direction_and_lexical_arm(self):
        rows = [
            {"bucket_id": "a", "cue_text": "a"},
            {"bucket_id": "b", "cue_text": "b"},
            {"bucket_id": "c", "cue_text": "c"},
            {"bucket_id": "d", "cue_text": "d"},
        ]
        terms = [{"common"}, {"common"}, {"common"}, {"common"}]
        features = (rows, terms, {"common": 4}, [0, 1, 2, 3],
                    np.asarray([[1., 0.], [0.9, 0.1], [0., 1.], [0.1, 0.9]]))
        ranked = AssocIndex._rank_directions(
            object(), ["common"], [[1., 0.], [0., 1.]], 4, features)
        self.assertEqual([row["bucket_id"] for row in ranked], ["a", "c", "d", "b"])

    def test_invalid_index_scatter_does_not_shift_or_poison_other_direction(self):
        engine = EmbeddingEngine.__new__(EmbeddingEngine)
        engine.model = "test-model"
        engine.max_chars = 1000
        engine.query_instruction = ""
        engine.document_instruction = ""
        engine.provider_routing = None
        engine._query_cache = {}
        engine._query_cache_ttl = 8.0
        engine.client = _BatchClient([
            SimpleNamespace(index=1, embedding=[0.0, 1.0]),
            SimpleNamespace(index=1, embedding=[8.0, 8.0]),
            SimpleNamespace(index=7, embedding=[1.0, 0.0]),
        ])
        result = asyncio.run(engine.generate_query_embeddings(["situation", "question"]))
        self.assertEqual(result, [None, None])
        self.assertEqual(len(engine.client.calls), 1)
        self.assertEqual(engine.client.calls[0]["input"], ["situation", "question"])

    def test_missing_scatter_row_is_none_and_valid_row_is_cached(self):
        engine = EmbeddingEngine.__new__(EmbeddingEngine)
        engine.model = "test-model"
        engine.max_chars = 1000
        engine.query_instruction = ""
        engine.document_instruction = ""
        engine.provider_routing = None
        engine._query_cache = {}
        engine._query_cache_ttl = 8.0
        engine.client = _BatchClient([SimpleNamespace(index=1, embedding=[0.0, 1.0])])
        result = asyncio.run(engine.generate_query_embeddings(["first", "second"]))
        self.assertEqual(result, [None, [0.0, 1.0]])
        self.assertEqual(len(engine._query_cache), 1)

    def test_nonfinite_vector_is_rejected(self):
        engine = EmbeddingEngine.__new__(EmbeddingEngine)
        engine.model = "test-model"
        engine.max_chars = 1000
        engine.query_instruction = ""
        engine.document_instruction = ""
        engine.provider_routing = None
        engine._query_cache = {}
        engine._query_cache_ttl = 8.0
        engine.client = _BatchClient([SimpleNamespace(index=0, embedding=[float("nan")])])
        self.assertEqual(asyncio.run(engine.generate_query_embeddings(["query"])), [None])
        self.assertEqual(engine._query_cache, {})


if __name__ == "__main__":
    unittest.main()

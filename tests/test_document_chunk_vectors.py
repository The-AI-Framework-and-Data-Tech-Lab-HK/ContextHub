"""Original ranges, atomic indexing, semantic discovery and evidence fusion."""
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from contexthub.errors import ServiceUnavailableError
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.retrieval.long_doc.coordinator import LongDocRetrievalCoordinator
from contexthub.retrieval.long_doc.ranges import document_ranges, embedding_input
from contexthub.retrieval.long_doc.result import LongDocRetrievalResult
from contexthub.retrieval.long_doc.vector_retriever import ChunkVectorRetriever
from contexthub.services.document_chunk_indexer import DocumentChunkIndexer


def sections_for(text):
    left = text.index('## First')
    right = text.index('## Second')
    return [dict(section_id=1, parent_id=None, node_id='root', title='Document', depth=0,
                 start_offset=0, end_offset=len(text)),
            dict(section_id=2, parent_id=1, node_id='heading-0001', title='Guide', depth=1,
                 start_offset=0, end_offset=len(text)),
            dict(section_id=3, parent_id=2, node_id='heading-0002', title='First', depth=2,
                 start_offset=left, end_offset=right),
            dict(section_id=4, parent_id=2, node_id='heading-0003', title='Second', depth=2,
                 start_offset=right, end_offset=len(text))]


def test_ranges_cover_source_once_and_keep_intro_and_chapter_boundaries():
    text = '# Guide\nIntroduction.\n\n## First\n' + 'A complete sentence. ' * 60 + '\n\n## Second\nFinal evidence.'
    sections = sections_for(text)
    ranges = document_ranges(text, sections, 100)
    assert ''.join(text[a:b] for a,b in ranges) == text
    assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
    assert all(a < b and b-a <= 100 for a,b in ranges)
    assert all(a[1] == b[0] for a,b in zip(ranges,ranges[1:]))
    for section in sections[2:]:
        assert all(not (a < section['start_offset'] < b) for a,b in ranges)
    start, end = ranges[-1]
    value = embedding_input(text, sections, start, end)
    assert value.startswith('Guide > Second\n\n')
    assert value.endswith(text[start:end])


@pytest.mark.asyncio
async def test_indexer_batches_originals_and_writes_no_bodies_or_summaries():
    text = '# Guide\nIntro.\n\n## First\nEvidence.\n\n## Second\nMore evidence.'
    inputs, writes = [], []
    class Embedding:
        async def embed_batch(self, texts):
            inputs.extend(texts)
            return [[1., 0.] for _ in texts]
    class DB:
        async def execute(self, sql, *args):
            assert 'l0_content' not in sql and 'summary' not in sql
            writes.append(args)
    document = {'id': uuid4(), 'version': 7}
    count = await DocumentChunkIndexer(Embedding()).index(DB(), document, text, sections_for(text))
    assert count == len(inputs) == len(writes) == 3
    assert all(args[0:2] == (document['id'], 7) for args in writes)
    assert all(args[5] == sha256(text.encode()).hexdigest() for args in writes)
    assert all(args[6] == '[1.0,0.0]' for args in writes)


@pytest.mark.asyncio
async def test_incomplete_embedding_batch_saves_nothing_and_noop_is_explicit():
    class Embedding:
        async def embed_batch(self, texts):
            return [[1.], None, [1.]]
    text = '# Guide\nIntro.\n\n## First\nOne.\n\n## Second\nTwo.'
    with pytest.raises(ServiceUnavailableError):
        await DocumentChunkIndexer(Embedding()).index(SimpleNamespace(), {'id': uuid4(), 'version': 1}, text, sections_for(text))
    assert await DocumentChunkIndexer(NoOpEmbeddingClient()).index(SimpleNamespace(), {}, text, []) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['retrieve', 'score_candidates'])
@pytest.mark.parametrize('changed', ['none', 'version', 'source'])
async def test_vector_reads_exact_evidence_and_rejects_changed_source_or_version(tmp_path, changed, operation):
    text = 'The original evidence.'
    (tmp_path/'extracted.txt').write_text(text if changed != 'source' else 'Replacement content.')
    doc = {'id': uuid4(), 'uri': 'ctx://resources/example', 'version': 4, 'file_path': str(tmp_path)}
    class DB:
        async def fetch(self, sql, *args):
            assert 'e.context_version = c.version' in sql
            assert "e.account_id = current_setting('app.account_id')" in sql
            assert args[1] == [doc['id']]
            if operation == 'score_candidates':
                assert 'unnest($3::uuid[], $4::int[], $5::int[])' in sql
                assert 'LIMIT' not in sql
                assert args[2:] == ([doc['id']], [4], [len(text)])
            return [dict(context_id=doc['id'], context_version=3 if changed=='version' else 4,
                         section_id=9,start_offset=4,end_offset=len(text),
                         source_sha256=sha256(text.encode()).hexdigest(),similarity=0.75)]
    retriever = ChunkVectorRetriever()
    if operation == 'retrieve':
        results = await retriever.retrieve(DB(), [1.], [doc], 3)
    else:
        candidate = LongDocRetrievalResult(doc['id'], doc['uri'], 'keyword', 9, text[4:], (4,len(text)), 2.)
        results = await retriever.score_candidates(DB(), [1.], [doc], [candidate])
    if changed != 'none':
        assert results == []
    else:
        assert results[0].snippet == text[4:]
        assert results[0].snippet_offset == (4,len(text))
        assert results[0].strategy == 'vector'


@pytest.mark.asyncio
async def test_fuse_same_range_without_overwriting_different_range_by_route_order():
    doc = {'id': uuid4(), 'uri': 'ctx://resources/example', 'version': 1}
    good = LongDocRetrievalResult(doc['id'], doc['uri'], 'keyword', 7, 'first evidence', (0,14), 8)
    other = replace(good, strategy='vector', section_id=8, snippet='different evidence', snippet_offset=(15,33), relevance_score=.9)
    class Keyword:
        async def retrieve(self, *args): return [good]
    class Vector:
        async def retrieve(self, *args): return [other, replace(good,strategy='vector',relevance_score=.8)]
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy('keyword', Keyword())
    coordinator.register_strategy('vector', Vector())
    hits = await coordinator.search_originals(None,'query',[doc],query_embedding=[1.])
    assert [hit['snippet'] for hit in hits] == ['different evidence', 'first evidence']
    assert hits[0]['retrieval_strategy'] == 'vector'
    assert hits[1]['retrieval_strategy'] == 'hybrid'
    assert hits[0]['_rerank_score'] == pytest.approx(0.8)
    assert hits[1]['_rerank_score'] == pytest.approx(0.2)
    assert len({hit['snippet_offset'] for hit in hits}) == 2


@pytest.mark.asyncio
async def test_semantic_original_route_does_not_require_keyword_or_l0_match():
    doc = {'id': uuid4(), 'uri': 'ctx://resources/example', 'version': 1}
    class Keyword:
        async def retrieve(self, *args): return []
    class Vector:
        async def retrieve(self, db, vector, documents, top_k):
            assert vector == [1.] and documents == [doc]
            return [LongDocRetrievalResult(doc['id'],doc['uri'],'vector',2,'original',(0,8),.8)]
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy('keyword', Keyword())
    coordinator.register_strategy('vector', Vector())
    hits = await coordinator.search_originals(None,'paraphrase',[doc],query_embedding=[1.])
    assert hits[0]['snippet'] == 'original'


@pytest.mark.asyncio
async def test_fusion_completes_only_missing_cosines_and_preserves_candidate_union():
    doc = {'id': uuid4(), 'uri': 'ctx://resources/example', 'version': 7}
    a = LongDocRetrievalResult(doc['id'], doc['uri'], 'vector', 1, 'a', (0,1), .9)
    b = replace(a, section_id=2, snippet='b', snippet_offset=(2,3), relevance_score=.8)
    c = replace(a, strategy='keyword', section_id=3, snippet='c', snippet_offset=(4,5), relevance_score=5.)
    class Keyword:
        async def retrieve(self, *args):
            return [replace(b, strategy='keyword', relevance_score=10.), c]
    class Vector:
        async def retrieve(self, db, query_embedding, documents, top_k):
            assert top_k == 2
            return [a, b]
        async def score_candidates(self, db, query_embedding, documents, candidates):
            assert candidates == [c] and documents == [doc] and query_embedding == [1.]
            return [replace(c, strategy='vector', relevance_score=.7)]
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy('keyword', Keyword())
    coordinator.register_strategy('vector', Vector())
    hits = await coordinator.search_originals(None, 'query', [doc], query_embedding=[1.], top_k=2)
    assert [h['snippet'] for h in hits] == ['a', 'b', 'c']
    # Cosine min/max use all three union candidates (.7/.9), including the
    # completed score. BM25 min/max include vector-only a's genuine zero (0/10).
    assert [h['_rerank_score'] for h in hits] == pytest.approx([.8, .6, .1])
    assert [h['retrieval_strategy'] for h in hits] == ['vector', 'hybrid', 'hybrid']
    assert len({(h['id'],h['version'],h['snippet_offset']) for h in hits}) == 3


@pytest.mark.asyncio
async def test_missing_stored_vector_is_not_a_zero_cosine_in_normalization():
    doc = {'id': uuid4(), 'uri': 'ctx://resources/example', 'version': 1}
    a = LongDocRetrievalResult(doc['id'], doc['uri'], 'vector', 1, 'a', (0,1), -.8)
    b = replace(a, section_id=2, snippet='b', snippet_offset=(2,3), relevance_score=-.2)
    c = replace(a, strategy='keyword', section_id=3, snippet='c', snippet_offset=(4,5), relevance_score=5.)
    class Keyword:
        async def retrieve(self, *args): return [c]
    class Vector:
        async def retrieve(self, *args): return [b, a]
        async def score_candidates(self, *args): return []  # no valid stored vector
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy('keyword', Keyword())
    coordinator.register_strategy('vector', Vector())
    hits = await coordinator.search_originals(None, 'query', [doc], query_embedding=[1.])
    assert [h['snippet'] for h in hits] == ['b', 'c', 'a']
    assert [h['_rerank_score'] for h in hits] == pytest.approx([.8, .2, 0.])
    assert hits[1]['retrieval_strategy'] == 'keyword'


@pytest.mark.asyncio
@pytest.mark.parametrize('scores', [(.4, .4), (.4, .9)])
async def test_constant_or_missing_route_adds_no_ranking_distinction(scores):
    doc = {'id': uuid4(), 'uri': 'ctx://resources/example', 'version': 1}
    a = LongDocRetrievalResult(doc['id'], doc['uri'], 'keyword', 1, 'a', (0,1), 3.)
    b = replace(a, section_id=2, snippet='b', snippet_offset=(2,3))
    class Keyword:
        async def retrieve(self, *args): return [a, b]  # constant BM25 scores
    class Vector:
        async def retrieve(self, *args):
            return [replace(r, strategy='vector', relevance_score=score) for r,score in zip((a,b),scores)]
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy('keyword', Keyword())
    coordinator.register_strategy('vector', Vector())
    hits = await coordinator.search_originals(None, 'query', [doc], query_embedding=[1.])
    if scores[0] == scores[1]:
        assert [h['_rerank_score'] for h in hits] == [0.,0.]
    else:
        assert [h['snippet'] for h in hits] == ['b','a']
        assert [h['_rerank_score'] for h in hits] == [.8,0.]
    # No query vector: the single available route remains usable, without an
    # embedding attempt or artificial cosine score. Its tied order is stable.
    hits = await coordinator.search_originals(None, 'query', [doc])
    assert [h['snippet'] for h in hits] == ['a','b']
    assert all(h['retrieval_strategy']=='keyword' for h in hits)


@pytest.mark.asyncio
async def test_rrf_keeps_original_ranks_and_does_not_complete_vector_votes():
    doc = {'id': uuid4(), 'uri': 'ctx://resources/selection', 'version': 1}
    lexical = LongDocRetrievalResult(doc['id'], doc['uri'], 'keyword', 1, 'lexical', (0,7), 10.)
    shared = replace(lexical, section_id=2, snippet='shared', snippet_offset=(8,14), relevance_score=2.)
    semantic = replace(lexical, strategy='vector', section_id=3, snippet='semantic', snippet_offset=(15,23), relevance_score=.9)
    class Keyword:
        async def retrieve(self, *args): return [lexical, shared]
    class Vector:
        async def retrieve(self, *args):
            return [semantic, replace(shared,strategy='vector',relevance_score=.8)]
        async def score_candidates(self, *args):
            pytest.fail('RRF must not invent vector-rank votes through score completion')
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy('keyword', Keyword())
    coordinator.register_strategy('vector', Vector())
    hits = await coordinator.search_originals(None,'query',[doc],query_embedding=[1.],original_fusion='rrf')
    assert [h['snippet'] for h in hits] == ['shared','lexical','semantic']
    assert [h['_rerank_score'] for h in hits] == pytest.approx([2/62,1/61,1/61])
    assert hits[0]['retrieval_strategy'] == 'hybrid'
    assert len({h['snippet_offset'] for h in hits}) == 3

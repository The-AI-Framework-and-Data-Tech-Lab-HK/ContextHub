# 11 — 长文档结构化检索（可插拔扩展）

## 问题本质

ContextHub 的 L0/L1/L2 模型对长文档有一个隐含假设：文档会被预处理成多条 context 记录（L0 ~100 tokens, L1 ~2k tokens）。这本质上是 chunking——固定粒度切分会截断语义边界，向量相似度搜索会丢失文档结构信息。

对于企业级结构化长文档（财报、法规、技术手册，数百至上千页），存在两种已验证的替代路径：
- **树导航检索**：借鉴 PageIndex，按文档自身章节层级构建树，LLM 沿树推理定位（FinanceBench 98.7% 准确率）
- **无索引关键词检索**：借鉴 sirchmunk，ripgrep 直搜文件 + Monte Carlo 采样提取证据窗口

本文档定义如何将这两种策略作为独立于 PG 核心存储的可插拔检索扩展引入 ContextHub。

## 设计原则

- **存算分离**：原文留在文件系统（ripgrep 可直搜），元数据和树结构存 PG
- **可插拔**：作为独立检索策略注册到 RetrievalRouter，Agent 的 context retrieval tool 按需选择
- **最小侵入**：不修改 `contexts` 核心表结构，不往 PG 塞大文本
- **保留原生能力**：sirchmunk 的 ripgrep、PageIndex 的树推理，各自在最擅长的层面工作

## 为什么长文档不存 PG

长文档与 ContextHub 其他 context 类型有本质区别：

| 特征 | skill / memory / 表元数据 | 长文档 |
|------|--------------------------|--------|
| 更新频率 | 高（Agent 持续写入） | 低（入库后基本不变） |
| 需要 ACID 事务 | 是（版本、传播、反馈联动） | 否 |
| 需要 RLS 租户隔离 | 是（多 Agent 多团队） | 文件系统权限 + ACL 表即可 |
| 内容大小 | KB 级 | MB 级 |
| 最优检索方式 | 向量相似度 + 结构化查询 | ripgrep 直搜 / 树导航 |

把 MB 级低频不变的内容塞进 PG TOAST，唯一收益是"统一存储"。代价是：
- TOAST 解压开销（任何 `substring()` 都要先解压）
- 丧失 ripgrep 直搜能力（sirchmunk 的核心优势）
- PG `tsvector` 是词级索引，精度和灵活性不如 ripgrep 的字节级正则搜索

## (1) 存储架构：文件系统 + PG 元数据

```
文件系统（原文，ripgrep 可直搜）          PG（元数据 + 树结构）
──────────────────────────              ─────────────────────
{doc_store_root}/                       contexts 表:
  {uri_hash}/                             uri = 'ctx://resources/finance/10k-2025'
    ├── source.pdf          ←───────────  context_type = 'long_document'
    ├── extracted.txt                     l0_content = "Apple 2025 年报..."（~100 tokens）
    └── extracted.md                      l1_content = "目录概览..."（~2k tokens）
                                          l2_content = NULL  ← 不存原文
                                          file_path = '/data/docs/{uri_hash}/'

                                        document_sections 表:
                                          树结构 + 章节摘要 + 字符偏移量
```

### PG 侧：contexts 表扩展

仅新增一个 `file_path` 列，指向文件系统路径。不加 `tsvector`，不存大文本：

```sql
ALTER TABLE contexts ADD COLUMN file_path TEXT;
-- 仅 long_document 类型使用此列
```

### PG 侧：文档树结构表

```sql
CREATE TABLE document_sections (
    section_id      SERIAL PRIMARY KEY,
    context_uri     TEXT NOT NULL REFERENCES contexts(uri),
    parent_id       INT REFERENCES document_sections(section_id),
    node_id         TEXT NOT NULL,               -- 树内编号（"0001", "0002"）
    title           TEXT NOT NULL,               -- 章节标题
    depth           INT NOT NULL DEFAULT 0,      -- 层级深度（0=根）
    start_offset    INT,                         -- 原文起始字符偏移
    end_offset      INT,                         -- 原文结束字符偏移
    summary         TEXT,                        -- LLM 生成的章节摘要（~100 tokens）
    token_count     INT,                         -- 该节点覆盖的 token 数
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_ds_context ON document_sections (context_uri);
CREATE INDEX idx_ds_parent ON document_sections (parent_id);
```

### 文件系统侧：文档存储

```python
DOC_STORE_ROOT = "/data/docs"  # 可配置

# 目录结构
# /data/docs/{uri_hash}/
#   source.pdf        — 原始文件
#   extracted.txt     — 纯文本提取（ripgrep 搜索目标）
#   extracted.md      — Markdown 提取（保留标题层级，树构建输入）
```

## (2) Ingestion 管线

```
source.pdf
    │
    ▼
文本提取（PyMuPDF / kreuzberg）
    │
    ├──→ extracted.txt  写入文件系统
    ├──→ extracted.md   写入文件系统
    │
    ▼
LLM 生成 L0/L1 + 构建文档树
    │
    ├──→ contexts 表:  L0, L1, file_path     写入 PG
    └──→ document_sections 表: 树节点        写入 PG
```

```python
class LongDocumentIngester:
    """长文档入库：文件 → 文件系统 + PG 元数据"""

    async def ingest(self, uri: str, source_path: str):
        uri_hash = hashlib.sha256(uri.encode()).hexdigest()[:16]
        doc_dir = Path(DOC_STORE_ROOT) / uri_hash
        doc_dir.mkdir(parents=True, exist_ok=True)

        # 1. 文件提取 → 写入文件系统
        shutil.copy(source_path, doc_dir / "source.pdf")
        text = extract_text(source_path)          # PyMuPDF
        md = extract_markdown(source_path)         # 保留标题层级
        (doc_dir / "extracted.txt").write_text(text)
        (doc_dir / "extracted.md").write_text(md)

        # 2. LLM 生成 L0/L1
        l0 = await self.llm.generate_l0(text)
        l1 = await self.llm.generate_l1(text)

        # 3. 构建文档树（应用层 LLM 调用，借鉴 PageIndex）
        tree = await self.build_document_tree(text, md)

        # 4. 写入 PG（元数据 + 树，不含原文）
        async with self.pg.transaction():
            await self.pg.execute("""
                INSERT INTO contexts (uri, context_type, scope,
                                     l0_content, l1_content, l2_content, file_path)
                VALUES ($1, 'long_document', 'resources', $2, $3, NULL, $4)
            """, uri, l0, l1, str(doc_dir))
            await self.persist_tree(uri, tree)

    async def build_document_tree(self, text: str, md: str) -> list[SectionNode]:
        """
        借鉴 PageIndex 的三种模式：
        - 有 TOC + 页码：提取 TOC 层级，映射到字符偏移
        - 有 TOC 无页码：LLM 定位各节起始位置
        - 无 TOC：LLM 从 Markdown 标题层级生成结构
        超过 max_token_per_node 的节点递归细分
        """
        toc = await self.llm.detect_toc(text[:TOKEN_LIMIT])
        if toc:
            sections = await self.llm.extract_hierarchy(toc, text)
        else:
            sections = await self.llm.generate_hierarchy_from_md(md)

        for section in sections:
            section.summary = await self.llm.summarize(
                text[section.start_offset:section.end_offset])
        return sections
```

## (3) 检索层：策略路由 + 双路径

Agent 的 context retrieval tool 调用 RetrievalRouter，路由器根据 context_type 分发：

```python
class RetrievalRouter:
    """检索策略路由"""

    def __init__(self):
        self.strategies: dict[str, RetrievalStrategy] = {}

    def register(self, context_type: str, strategy: RetrievalStrategy):
        self.strategies[context_type] = strategy

    async def retrieve(self, query: str, scope: str, top_k: int = 5) -> list[RetrievalResult]:
        # 默认路径：向量检索（通用 context）
        results = await self.vector_retrieve(query, scope, top_k)

        # 检查 scope 下是否有注册了特殊策略的 context 类型
        doc_uris = await self.pg.fetch("""
            SELECT uri, file_path FROM contexts
            WHERE context_type = 'long_document' AND scope = $1
        """, scope)

        if doc_uris and 'long_document' in self.strategies:
            strategy = self.strategies['long_document']
            doc_results = await strategy.retrieve(query, doc_uris)
            results = self.merge_and_rank(results, doc_results)

        return results

# 注册长文档检索策略（应用启动时）
router.register('long_document', LongDocumentStrategy(
    tree_retriever=TreeRetriever(pg=pg, llm=llm),
    keyword_retriever=KeywordRetriever(rg_path="rga"),
))
```

### 路径 B-1：树导航检索

LLM 沿 PG 中的文档树推理定位，然后从文件系统读取目标章节原文：

```python
class TreeRetriever:
    """沿文档树推理定位 → 从文件系统读取原文片段"""

    async def retrieve(self, query: str, context_uri: str, file_path: str) -> str:
        # 1. 从 PG 加载顶层节点
        top_nodes = await self.pg.fetch("""
            SELECT section_id, title, summary, depth
            FROM document_sections
            WHERE context_uri = $1 AND depth = 1
            ORDER BY start_offset
        """, context_uri)

        # 2. LLM 逐层选择最相关节点
        selected = await self.llm.select_relevant_node(query, top_nodes)
        while True:
            children = await self.pg.fetch("""
                SELECT section_id, title, summary, depth, token_count,
                       start_offset, end_offset
                FROM document_sections WHERE parent_id = $1
            """, selected.section_id)
            if not children or selected.token_count <= MAX_CONTEXT_TOKENS:
                break
            selected = await self.llm.select_relevant_node(query, children)

        # 3. 从文件系统读取目标片段（不经过 PG）
        txt_path = Path(file_path) / "extracted.txt"
        with open(txt_path) as f:
            f.seek(selected.start_offset)
            text = f.read(selected.end_offset - selected.start_offset)

        return text
```

### 路径 B-2：ripgrep 关键词 + Monte Carlo 窗口提取

直接在文件系统上用 ripgrep 搜索，保留 sirchmunk 的全部能力：

```python
class KeywordRetriever:
    """ripgrep 关键词搜索 + Monte Carlo 证据采样"""

    async def retrieve(self, query: str, doc_paths: list[str]) -> list[str]:
        # 1. LLM 提取多层级关键词（借鉴 sirchmunk）
        keywords = await self.llm.extract_keywords(query)

        # 2. ripgrep 并发搜索文件系统（不经过 PG）
        all_hits = []
        for kw_group in keywords:
            hits = await self.rga_search(kw_group, doc_paths)
            all_hits.extend(hits)

        # 3. 按文件聚合 + 排序
        ranked_files = self.rank_by_hits(all_hits)

        # 4. Monte Carlo 证据采样（借鉴 sirchmunk）
        evidence_windows = []
        for file_path, hit_positions in ranked_files[:TOP_K_FILES]:
            windows = await self.monte_carlo_sample(file_path, hit_positions)
            evidence_windows.extend(windows)

        # 5. LLM 合成证据
        evidence = await self.llm.synthesize_evidence(query, evidence_windows)
        return evidence

    async def rga_search(self, keywords: list[str], doc_paths: list[str]) -> list[Hit]:
        """调用 ripgrep-all，返回命中位置"""
        pattern = "|".join(re.escape(kw) for kw in keywords)
        txt_files = [str(Path(p) / "extracted.txt") for p in doc_paths]
        proc = await asyncio.create_subprocess_exec(
            "rga", "--json", "-e", pattern, *txt_files,
            stdout=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        return parse_rga_json(stdout)

    async def monte_carlo_sample(
        self, file_path: str, hit_positions: list[int],
        window_size: int = 2000, n_samples: int = 5
    ) -> list[str]:
        """三阶段采样：探索 → 利用 → 合成"""
        text = Path(file_path, "extracted.txt").read_text()

        # Phase 1: 在命中点周围开窗口（探索）
        seed_windows = []
        for pos in hit_positions:
            start = max(0, pos - window_size // 2)
            seed_windows.append(text[start:start + window_size])

        # Phase 2: 高斯重要性采样（利用）
        scored = await self.llm.score_windows(seed_windows)
        top_seeds = sorted(scored, key=lambda x: x.score, reverse=True)[:3]
        expanded = []
        for seed in top_seeds:
            for _ in range(n_samples):
                offset = int(random.gauss(seed.center, window_size * 0.3))
                offset = max(0, min(offset, len(text) - window_size))
                expanded.append(text[offset:offset + window_size])

        return seed_windows + expanded
```

## (4) 与现有架构的集成点

| 现有组件 | 集成方式 | 改动量 |
|----------|----------|--------|
| `contexts` 表 | 新增 `file_path` 列，`context_type='long_document'`，`l2_content=NULL` | ALTER TABLE 加一列 |
| ContextStore URI 路由 | `ctx://resources/{project}/{doc}` 下的长文档走新检索路径 | RetrievalRouter 注册策略 |
| L0/L1 生成 | 长文档的 L0/L1 照常生成（向量检索仍可发现文档） | 无改动 |
| 变更传播（06） | 文档更新时触发文件替换 + 树重建，传播规则同现有 | 新增 `doc_tree_rebuild` 规则 |
| 反馈与生命周期（07） | 长文档检索结果同样进入反馈循环 | 无改动 |
| 权限与审计（05） | 文档级 ACL 通过 `contexts` 表的现有机制控制，文件系统权限与 PG ACL 保持一致 | 无改动 |

### 数据流对比

```
现有路径（skill/memory/表元数据）：
  向量搜 L0 → PG 加载 L1 rerank → PG 加载 L2 全文

长文档路径 B-1（树导航）：
  向量搜 L0 发现文档 → PG 加载树节点 → LLM 逐层推理 → 文件系统读取目标片段

长文档路径 B-2（关键词窗口）：
  向量搜 L0 发现文档 → ripgrep 搜文件系统 → Monte Carlo 采样 → LLM 合成证据

两条路径的共同点：L0 向量检索作为"发现层"，确定哪些文档可能相关；
区别在于"精确定位层"——树导航走 PG，关键词搜索走文件系统。
```

### 依赖关系

```
11-long-document-retrieval
    依赖：01-storage-paradigm（contexts 表 + file_path 列）
    依赖：02-information-model（L0/L1 模型，向量发现层）
    依赖：06-change-propagation（文档更新 → 树重建传播）
    被依赖：09-implementation-plan（Phase 2 Track A 可选模块）
```

## (5) 量化验证方案

### 验证目标

证明文件系统原文 + PG 树结构的混合架构，在准确率、token 效率和检索延迟上优于传统 chunk + 向量检索。

### 基准对照组

| 策略 | 原文存储 | 检索机制 |
|------|----------|----------|
| **Baseline: chunk + vector** | PG（切分后的 chunks） | 向量嵌入 → top-K 相似度 → LLM 回答 |
| **Strategy A: 树导航** | 文件系统 | PG 树节点 → LLM 逐层推理 → 文件读取 → LLM 回答 |
| **Strategy B: ripgrep 窗口** | 文件系统 | ripgrep 关键词搜索 → Monte Carlo 采样 → LLM 回答 |
| **Strategy A+B: 混合** | 文件系统 | 树导航粗定位 → ripgrep 精定位 → LLM 回答 |

### 评估指标

| 指标 | 定义 | 测量方法 |
|------|------|----------|
| **Answer Accuracy** | 回答与标准答案的一致性 | LLM-as-Judge（GPT-4o 评分 1-5）+ 人工抽检 |
| **Retrieval Precision@K** | top-K 检索片段中包含正确答案的比例 | 标注数据集，检查片段是否覆盖答案所在段落 |
| **Token Efficiency** | 每次检索消耗的总 LLM token | 累计 ingestion + retrieval 阶段 |
| **Latency (P50/P95)** | 端到端检索延迟 | 查询发出 → 返回证据片段 |
| **Context Utilization** | 送入 LLM 的上下文中有效信息占比 | 有效 token / 总输入 token |

### 测试数据集

| 数据集 | 文档类型 | 规模 | 用途 |
|--------|----------|------|------|
| **FinanceBench** | 上市公司财报（10-K/10-Q） | 150 问题，跨 50+ 份报告 | 树导航在结构化文档上的准确率 |
| **内部标注集** | 企业技术手册 / 法规文件 | 自建 50-100 问题 | 目标领域实际效果 |
| **长文档压力测试** | 500+ 页单文档 | 3-5 份 | ripgrep vs tsvector 的性能对比 |

### 预期结果

| 指标 | Baseline (chunk+vector) | A: 树导航 | B: ripgrep 窗口 | A+B: 混合 |
|------|------------------------|-----------|-----------------|-----------|
| Accuracy | ~75-80% | ~95%+ | ~85-90% | ~95%+ |
| Precision@5 | ~60% | ~90% | ~75% | ~90%+ |
| Token/query | ~2000 | ~800 | ~1200 | ~1000 |
| Latency P50 | ~2s | ~3-5s | ~0.5-1s | ~2-3s |
| Context Util | ~30% | ~70% | ~50% | ~75% |

树导航准确率高但延迟高（多轮 LLM）；ripgrep 窗口速度快但准确率略低。混合策略：树导航缩小范围后 ripgrep 精定位，兼顾两者优势。

### 额外验证：存储架构对比

单独验证"文件系统 vs PG TOAST"的检索性能差异：

| 操作 | 文件系统 + ripgrep | PG TOAST + tsvector |
|------|-------------------|---------------------|
| 关键词搜索（500 页文档） | rga: ~50-200ms | ts_query: ~200-500ms（含 TOAST 解压） |
| 按偏移读取 10KB 片段 | fseek + fread: <1ms | substring(): ~50-100ms（TOAST 解压整块） |
| 并发搜索 10 份文档 | 并行 rga 进程: ~200ms | 10 次 ts_query: ~2-5s |

此对比用于验证存算分离架构的性能优势是否成立。

# Qdrant User Guide

> A comprehensive, practical guide for developers, platform engineers, and AI application builders.
> Covers Qdrant v1.17.x (March 2026). Written for a technical audience.

---

## Table of Contents

- [1. Introduction](#1-introduction)
- [2. Qdrant Fundamentals](#2-qdrant-fundamentals)
- [3. Architecture](#3-architecture)
- [4. Key Features](#4-key-features)
- [5. Installation and Setup](#5-installation-and-setup)
- [6. First Steps](#6-first-steps)
- [7. Data Modeling and Collection Design](#7-data-modeling-and-collection-design)
- [8. Indexing and Search Mechanics](#8-indexing-and-search-mechanics)
- [9. API and SDK Usage](#9-api-and-sdk-usage)
- [10. Advanced Features](#10-advanced-features)
- [11. Production Deployment Guidance](#11-production-deployment-guidance)
- [12. Qdrant for RAG and AI Systems](#12-qdrant-for-rag-and-ai-systems)
- [13. Performance Optimization](#13-performance-optimization)
- [14. Common Pitfalls](#14-common-pitfalls)
- [15. Comparison and Tradeoffs](#15-comparison-and-tradeoffs)
- [16. Best Practices Checklist](#16-best-practices-checklist)
- [17. Quick Start Recap](#17-quick-start-recap)
- [18. References](#18-references)
- [Appendix A: Feature Summary](#appendix-a-feature-summary)
- [Appendix B: Recommended Learning Path](#appendix-b-recommended-learning-path)
- [Appendix C: Top 10 Implementation Tips](#appendix-c-top-10-implementation-tips)

---

## 1. Introduction

### What Qdrant Is

Qdrant (pronounced "quadrant") is an open-source vector similarity search engine and database purpose-built for AI applications. Written in Rust and licensed under Apache 2.0, it is developed by Qdrant Solutions GmbH (Berlin, Germany).

| Attribute | Value |
|-----------|-------|
| **Language** | Rust |
| **Current Version** | v1.17.1 (March 2026) |
| **License** | Apache License 2.0 |
| **Company** | Qdrant Solutions GmbH |
| **GitHub Stars** | ~29,900 |
| **Protocols** | REST (port 6333), gRPC (port 6334) |

Qdrant stores, indexes, and searches high-dimensional vectors with attached JSON metadata (called "payloads"). Its core search algorithm is HNSW (Hierarchical Navigable Small World), extended with a proprietary filterable HNSW implementation that maintains search quality even under heavy filtering.

### Who It Is For

- **AI/ML engineers** building RAG pipelines, semantic search, or recommendation systems
- **Backend developers** adding vector search capabilities to applications
- **Platform engineers** deploying and operating vector infrastructure at scale
- **Data scientists** prototyping similarity-based workflows

### Problems It Solves

1. **Semantic retrieval** — Find items by meaning, not exact keywords
2. **Scale** — Handle billions of vectors with sub-100ms latency
3. **Rich filtering** — Combine vector similarity with structured metadata constraints
4. **Hybrid search** — Merge dense (semantic) and sparse (keyword) retrieval in a single query
5. **Multi-modal search** — Store and query text, image, audio, and code embeddings side by side

### High-Level Feature Summary

| Category | Highlights |
|----------|------------|
| **Search** | ANN (HNSW), exact search, hybrid search, recommendation, discovery |
| **Vectors** | Dense, sparse, named (multiple per point), multivector |
| **Filtering** | Boolean logic, range, geo, full-text, datetime, nested objects |
| **Quantization** | Scalar (int8), product, binary, 1.5-bit, 2-bit |
| **Scaling** | Sharding, replication, Raft consensus, distributed mode |
| **Storage** | In-memory, mmap, on-disk — configurable per collection |
| **Security** | API keys, JWT RBAC, TLS, read-only keys |
| **Ecosystem** | SDKs for Python, TypeScript, Rust, Go, Java, .NET |

---

## 2. Qdrant Fundamentals

### Vector Databases Explained

A vector database is a specialized system for storing and querying high-dimensional numerical arrays (vectors). Traditional databases excel at exact lookups and range queries on structured data. Vector databases excel at **approximate nearest neighbor (ANN)** search — finding the most similar items in a high-dimensional space.

Typical workflow:

```
Raw Data → Embedding Model → Vectors → Vector Database → Similarity Search → Results
```

### How Qdrant Works Conceptually

1. You create a **collection** with a defined vector dimensionality and distance metric
2. You insert **points** — each point is a vector + optional metadata (payload)
3. Qdrant builds an **HNSW graph index** over the vectors for fast approximate search
4. You query with a vector; Qdrant traverses the graph to find the nearest neighbors
5. Optionally, you attach **filters** on payload fields to constrain results

### Core Terminology

| Term | Definition |
|------|-----------|
| **Collection** | A named set of points sharing the same vector configuration |
| **Point** | The fundamental data unit: unique ID + vector(s) + optional payload |
| **Payload** | JSON metadata attached to a point (e.g., `{"category": "tech", "date": "2025-01-01"}`) |
| **Segment** | Internal storage unit holding a subset of a collection's points, indexes, and data structures |
| **Shard** | A horizontal partition of a collection, distributed across nodes in a cluster |
| **HNSW** | Hierarchical Navigable Small World — the graph-based ANN index algorithm |
| **Named Vector** | A labeled vector space within a point, allowing multiple embeddings per point |
| **Sparse Vector** | A vector where most dimensions are zero, stored as index-value pairs |

### Dense vs. Sparse Vectors

| Aspect | Dense Vectors | Sparse Vectors |
|--------|--------------|----------------|
| **Representation** | Fixed-length array of floats (e.g., 768 dims) | Variable-length list of (index, value) pairs |
| **Source** | Neural embedding models (OpenAI, Cohere, etc.) | Lexical models (SPLADE, BM25) or TF-IDF |
| **Captures** | Semantic meaning, context, nuance | Keyword importance, exact term matches |
| **Storage** | Fixed memory per vector | Proportional to non-zero elements |
| **Search type** | Semantic similarity | Keyword/lexical matching |

Qdrant supports both in the same collection via named vectors, enabling hybrid search.

### Similarity Search Basics

Given a query vector **q** and a database of vectors, similarity search finds the **k** vectors most similar to **q** according to a distance metric. Exact search (brute-force) scans every vector — O(n). ANN search uses index structures like HNSW to achieve sub-linear time at the cost of a small recall loss.

---

## 3. Architecture

### Internal Architecture Overview

```
Qdrant Instance
  └── TableOfContent (TOC) — storage orchestrator
       └── Collection — named set of points sharing vector config
            └── Shard — independent partition (consistent hashing or custom)
                 └── Segment — stores data structures for a subset of points
                      ├── Vector Index (HNSW graph)
                      ├── Payload Index (keyword, integer, geo, text, etc.)
                      ├── ID Mapper
                      └── Vector Storage (RAM / mmap / on-disk)
```

### Collections

A collection is the top-level organizational unit. All points in a collection share:
- The same vector dimensionality (or named vector configuration)
- The same distance metric
- Common HNSW, optimizer, and quantization settings

Collections are managed by the **TableOfContent (TOC)**, Qdrant's internal storage orchestrator.

### Points

A point is the atomic data unit, consisting of:

- **ID**: Unique identifier — either a 64-bit unsigned integer or a UUID string
- **Vector(s)**: One or more embedding vectors (dense, sparse, or both via named vectors)
- **Payload**: Optional JSON object with arbitrary metadata

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "vector": [0.1, 0.2, 0.3, ...],
  "payload": {
    "title": "Introduction to Qdrant",
    "category": "documentation",
    "created_at": "2025-06-15T10:30:00Z"
  }
}
```

### Payload

Payloads are schemaless JSON objects. Any JSON-compatible structure works — strings, numbers, booleans, arrays, nested objects. Qdrant does not enforce a schema, but you must create **payload indexes** on fields you plan to filter on for acceptable performance.

Supported payload index types:

| Index Type | Data Type | Use Case |
|------------|-----------|----------|
| `keyword` | String | Exact match on categorical values |
| `integer` | Integer | Range and exact match |
| `float` | Float | Range queries |
| `bool` | Boolean | Boolean match |
| `geo` | Geo point | Geographic queries |
| `text` | Text | Full-text search (tokenized) |
| `datetime` | Datetime | Temporal range queries |
| `uuid` | UUID | UUID exact match |

### Segments

Segments are internal storage units within a shard. Each segment contains:
- A subset of the collection's points
- Its own HNSW graph index
- Payload indexes for its points
- Vector storage (in the configured mode: RAM, mmap, or on-disk)
- An ID mapper

**Key tradeoff**: More segments = faster indexing (smaller indexes to build). Fewer, larger segments = faster search (fewer indexes to scan). The background **optimizer** merges small segments into larger ones over time.

### Indexes

**Vector Index (HNSW):**
- Multi-layered graph where each node is a vector
- Search traverses from coarse upper layers to fine lower layers
- Qdrant's **filterable HNSW** extends the graph with edges corresponding to payload values, enabling filtered search without post-filtering penalty

**Payload Indexes:**
- Secondary indexes on payload fields
- Without a payload index, Qdrant loads entire payloads from disk to evaluate filter conditions — a major performance bottleneck

### Storage Model

| Mode | Description | Trade-off |
|------|-------------|-----------|
| **In-Memory** | Vectors stored entirely in RAM | Fastest; requires sufficient RAM |
| **Memmap (mmap)** | Memory-mapped files; OS manages page cache | Large datasets; hot vectors cached in RAM automatically |
| **On-Disk** | Vectors read from disk on demand | Lowest RAM usage; highest latency |

The `memmap_threshold` parameter controls when segments switch from in-memory to mmap. You can also set `on_disk: true` explicitly per collection.

### Search Execution Flow

1. Client sends query vector + optional filters to REST/gRPC endpoint
2. Qdrant routes to relevant shard(s)
3. Within each shard, each segment is searched:
   a. If filters are present and payload indexes exist, Qdrant uses filterable HNSW
   b. HNSW graph traversal identifies candidate nearest neighbors
   c. Candidates are scored and ranked
4. Results from all segments are merged
5. In distributed mode, results from all shards/replicas are merged
6. Top-k results returned to client

---

## 4. Key Features

### ANN Search

Qdrant's core capability. HNSW-based approximate nearest neighbor search with configurable recall/latency tradeoff via the `ef` search parameter. Also supports exact (brute-force) search for small collections or when perfect recall is required.

```python
# ANN search (default)
results = client.query_points(
    collection_name="docs",
    query=[0.1, 0.2, ...],
    limit=10,
)

# Exact search (brute-force)
results = client.query_points(
    collection_name="docs",
    query=[0.1, 0.2, ...],
    limit=10,
    search_params=SearchParams(exact=True),
)
```

### Filtering

Rich payload filtering with boolean logic operators:

- **`must`** — All conditions must match (AND)
- **`should`** — At least one condition must match (OR)
- **`must_not`** — No conditions must match (NOT)
- **`min_should`** — Minimum N of `should` conditions must match

Filter condition types include: `match` (exact), `range` (numeric/datetime), `geo_bounding_box`, `geo_radius`, `geo_polygon`, `values_count`, `is_empty`, `is_null`, `has_id`, `nested`, `full_text_match`, and `datetime_range`.

### Payload Indexing

Create indexes on payload fields to accelerate filtered searches. Without indexes, Qdrant must load and scan payloads from disk.

```python
client.create_payload_index(
    collection_name="docs",
    field_name="category",
    field_schema="keyword",
)
```

For multitenancy, mark tenant fields with `is_tenant=True` to trigger storage co-location optimization:

```python
client.create_payload_index(
    collection_name="docs",
    field_name="tenant_id",
    field_schema=PayloadSchemaType.KEYWORD,
    is_tenant=True,
)
```

### Hybrid Search

Combines dense and sparse vector search results via fusion methods:

- **RRF (Reciprocal Rank Fusion)**: Combines by rank position; ignores raw scores. Robust when score scales differ.
- **DBSF (Distribution-Based Score Fusion)**: Normalizes and combines actual scores.

The **prefetch** mechanism enables multi-stage pipelines in a single API call:

```python
from qdrant_client.models import Prefetch, FusionQuery, Fusion

results = client.query_points(
    collection_name="docs",
    prefetch=[
        Prefetch(query=[0.1, 0.2, ...], using="dense", limit=20),
        Prefetch(query=SparseVector(indices=[1, 42, 99], values=[0.5, 0.8, 0.3]), using="sparse", limit=20),
    ],
    query=FusionQuery(fusion=Fusion.RRF),
    limit=10,
)
```

### Sparse Vector Support

Store SPLADE, BM25, or TF-IDF representations as sparse vectors alongside dense embeddings:

```python
client.create_collection(
    collection_name="hybrid_docs",
    vectors_config={
        "dense": VectorParams(size=768, distance=Distance.COSINE),
    },
    sparse_vectors_config={
        "sparse": SparseVectorParams(),
    },
)
```

### Multitenancy

Two patterns:

1. **Payload-based** (recommended for many small tenants): Add `tenant_id` to each point, create a keyword index with `is_tenant=True`, filter by tenant in every query.
2. **Custom sharding** (for fewer, larger tenants): Assign points to specific shards by tenant, route queries to specific shards.

> **Warning:** Do not create a separate collection per tenant. This is a documented anti-pattern that causes significant resource overhead.

### Replication and Clustering

- **Sharding**: Automatic (consistent hashing) or user-defined (v1.7.0+)
- **Replication**: Configurable `replication_factor` per collection; each shard has a ReplicaSet
- **Consensus**: Raft protocol for cluster topology and collection-level operations
- **Point operations** (upsert, search, delete) use direct peer-to-peer communication — not Raft — for low overhead

### Snapshots and Backups

- **Collection snapshots**: All data, configuration, and indexes for one collection
- **Full snapshots**: All collections + alias mappings
- Create, download, and restore via API or CLI
- Optional SHA256 checksum verification

### Observability and Monitoring

- **Prometheus metrics endpoint**: `GET /metrics` on each node
- Key metrics: search latency (p50/p95/p99), upsert throughput, collection size, memory usage, segment counts
- Web dashboard at `http://localhost:6333/dashboard`

### API and SDK Ecosystem

| Language | Package | Protocol |
|----------|---------|----------|
| Python | `qdrant-client` | REST + gRPC |
| TypeScript | `@qdrant/js-client-rest` | REST |
| Rust | `qdrant-client` | gRPC |
| Go | `go-client` | gRPC |
| Java | `qdrant-client` | gRPC |
| .NET | `Qdrant.Client` | gRPC |

---

## 5. Installation and Setup

### Local Binary

Download from [GitHub Releases](https://github.com/qdrant/qdrant/releases) or build from source:

```bash
# Build from source (requires Rust toolchain)
git clone https://github.com/qdrant/qdrant.git
cd qdrant
cargo build --release
./target/release/qdrant
```

### Docker (Recommended for Development)

```bash
docker pull qdrant/qdrant

docker run -p 6333:6333 -p 6334:6334 \
    -v "$(pwd)/qdrant_storage:/qdrant/storage:z" \
    qdrant/qdrant
```

- **Port 6333**: REST API + Web Dashboard (`http://localhost:6333/dashboard`)
- **Port 6334**: gRPC API

### Docker Compose

```yaml
version: '3.8'
services:
  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - qdrant_data:/qdrant/storage
      - qdrant_snapshots:/qdrant/snapshots
    environment:
      - QDRANT__SERVICE__API_KEY=your-secret-api-key
    restart: unless-stopped

volumes:
  qdrant_data:
  qdrant_snapshots:
```

### Kubernetes (Helm Chart)

```bash
helm repo add qdrant https://qdrant.github.io/qdrant-helm
helm repo update
helm install qdrant qdrant/qdrant
```

The Helm chart supports StatefulSets, configurable replicas, resource limits, TLS, API key configuration, and PVC templates.

> **Note:** For production Kubernetes, Qdrant recommends their Private Cloud Enterprise Operator for zero-downtime upgrades, auto-scaling, and disaster recovery.

### Qdrant Cloud (Managed)

| Tier | Details |
|------|---------|
| **Free** | 1 GB RAM, 4 GB disk, no credit card |
| **Standard** | Pay-as-you-go; ~$150-200/month for 8 GB RAM, 2 vCPU |
| **Hybrid Cloud** | Starting at $0.014/hour; runs in your infrastructure |
| **Enterprise** | Custom contracts; $2,000-5,000+/month |

Available on AWS, Google Cloud, and Azure across multiple regions. Billing is hourly based on vCPU, RAM, storage, and backup volume.

### Verification Steps

After starting Qdrant:

```bash
# Check health
curl http://localhost:6333/healthz
# Expected: {"title":"qdrant - vectorass engine","version":"1.17.1"}

# Check dashboard
# Open http://localhost:6333/dashboard in a browser

# List collections (should be empty initially)
curl http://localhost:6333/collections
# Expected: {"result":{"collections":[]},"status":"ok","time":0.000...}
```

---

## 6. First Steps

### Create a Collection

```python
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

client = QdrantClient(host="localhost", port=6333)

client.create_collection(
    collection_name="articles",
    vectors_config=VectorParams(size=384, distance=Distance.COSINE),
)
```

### Insert Vectors with Metadata

```python
from qdrant_client.models import PointStruct

client.upsert(
    collection_name="articles",
    wait=True,
    points=[
        PointStruct(
            id=1,
            vector=[0.05, 0.61, 0.76, ...],  # 384-dim vector
            payload={
                "title": "Introduction to Vector Search",
                "category": "tutorial",
                "author": "Jane Doe",
                "published": "2025-03-15",
                "tags": ["search", "vectors", "ai"],
            },
        ),
        PointStruct(
            id=2,
            vector=[0.19, 0.81, 0.75, ...],
            payload={
                "title": "Building RAG Pipelines",
                "category": "guide",
                "author": "John Smith",
                "published": "2025-06-01",
                "tags": ["rag", "llm", "retrieval"],
            },
        ),
    ],
)
```

### Run a Similarity Search

```python
results = client.query_points(
    collection_name="articles",
    query=[0.2, 0.1, 0.9, ...],  # query vector
    limit=5,
    with_payload=True,
).points

for point in results:
    print(f"ID: {point.id}, Score: {point.score}, Title: {point.payload['title']}")
```

### Run a Filtered Search

```python
from qdrant_client.models import Filter, FieldCondition, MatchValue

results = client.query_points(
    collection_name="articles",
    query=[0.2, 0.1, 0.9, ...],
    query_filter=Filter(
        must=[
            FieldCondition(key="category", match=MatchValue(value="guide")),
        ]
    ),
    limit=5,
    with_payload=True,
).points
```

> **Tip:** Create a payload index on `category` before running filtered searches at scale:
> ```python
> client.create_payload_index("articles", "category", "keyword")
> ```

### Update and Delete Records

```python
# Update payload on existing points
client.set_payload(
    collection_name="articles",
    payload={"category": "advanced-guide"},
    points=[2],
)

# Delete specific points
client.delete(
    collection_name="articles",
    points_selector=[1],
)

# Delete by filter
from qdrant_client.models import FilterSelector
client.delete(
    collection_name="articles",
    points_selector=FilterSelector(
        filter=Filter(must=[FieldCondition(key="category", match=MatchValue(value="tutorial"))])
    ),
)
```

---

## 7. Data Modeling and Collection Design

### Choosing Collection Layouts

| Scenario | Approach |
|----------|----------|
| Single embedding model, one data type | Single collection, single vector |
| Multiple embedding models on same data | Single collection, named vectors |
| Dense + sparse for hybrid search | Single collection, dense named vector + sparse named vector |
| Completely different data domains | Separate collections |
| Multi-tenant SaaS | Single collection with `tenant_id` payload + index |

### Payload Schema Strategy

Qdrant payloads are schemaless — you can store any JSON. However, plan your payload structure upfront:

1. **Index what you filter on.** Every field used in a filter query needs a payload index.
2. **Keep payloads lean.** Store only metadata needed for filtering and result enrichment. Store large content (full text, images) externally.
3. **Use consistent field names and types.** Mixing types on the same field (e.g., string in some points, integer in others) causes index issues.
4. **Nest carefully.** Nested object filtering is supported but requires explicit nested filter syntax.

### Single vs. Multiple Collections

**Prefer fewer, larger collections:**
- Qdrant has per-collection overhead (HNSW graph, segment management, optimizer threads)
- Creating many small collections (e.g., one per user) is a documented anti-pattern
- Use payload-based filtering for logical separation within a single collection

**Use separate collections when:**
- Data has fundamentally different vector dimensions or distance metrics
- Data domains have zero overlap and will never be co-queried
- Isolation requirements demand separate storage/replication settings

### Multitenancy Patterns

**Pattern 1: Payload-based (recommended for <100K tenants)**

```python
# Create collection
client.create_collection(
    collection_name="multi_tenant",
    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
)

# Create tenant index with is_tenant optimization
client.create_payload_index(
    collection_name="multi_tenant",
    field_name="tenant_id",
    field_schema=PayloadSchemaType.KEYWORD,
    is_tenant=True,
)

# Always filter by tenant
results = client.query_points(
    collection_name="multi_tenant",
    query=query_vector,
    query_filter=Filter(must=[
        FieldCondition(key="tenant_id", match=MatchValue(value="tenant_abc")),
    ]),
    limit=10,
)
```

**Pattern 2: Custom sharding (for fewer, larger tenants)**

```python
client.create_collection(
    collection_name="sharded_tenant",
    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
    sharding_method=ShardingMethod.CUSTOM,
)

# Insert to specific shard
client.upsert(
    collection_name="sharded_tenant",
    shard_key_selector="tenant_abc",
    points=[...],
)

# Query specific shard
results = client.query_points(
    collection_name="sharded_tenant",
    query=query_vector,
    shard_key_selector="tenant_abc",
    limit=10,
)
```

### Vector Dimensionality Planning

| Embedding Model | Dimensions | Notes |
|----------------|------------|-------|
| OpenAI `text-embedding-3-small` | 1536 | Can be truncated to 512/1024 |
| OpenAI `text-embedding-3-large` | 3072 | Can be truncated |
| Cohere `embed-v4` | 1024 | |
| Sentence-Transformers `all-MiniLM-L6-v2` | 384 | Lightweight |
| Google `text-embedding-005` | 768 | |
| BGE-M3 | 1024 | Multilingual |

**Guidelines:**
- Higher dimensions capture more nuance but use more RAM and are slower to index/search
- Many models support Matryoshka (dimension truncation) — test if lower dims maintain acceptable recall for your use case
- Binary quantization works best with dimensions >= 1024

### Distance Metric Selection

| Metric | Best For | Notes |
|--------|----------|-------|
| **Cosine** | Most text embeddings | Direction-based; ignores magnitude. Default choice. |
| **Dot Product** | Pre-normalized embeddings | Slightly faster than Cosine when vectors are unit-length |
| **Euclidean** | Spatial data, coordinates | Sensitive to magnitude |
| **Manhattan** | High-dimensional with outliers | More robust than Euclidean in very high dims |

> **Tip:** If your embedding model normalizes output vectors (most do), Cosine and Dot Product produce equivalent rankings. Use Dot Product for a marginal speed advantage.

---

## 8. Indexing and Search Mechanics

### HNSW Indexing

Qdrant uses HNSW — a multi-layered graph where each node represents a vector. Key characteristics:

- **Build phase**: Vectors are inserted into the graph; each vector connects to `m` nearest neighbors at each layer
- **Search phase**: Starting from the top layer, greedily traverse toward the query vector, descending to finer layers for precision
- **Filterable HNSW**: Qdrant's proprietary extension adds edges corresponding to payload values, enabling filtered search within the graph traversal (not as a post-filter step)

#### HNSW Parameters

| Parameter | Default | Purpose | Tuning Guidance |
|-----------|---------|---------|-----------------|
| `m` | 16 | Max edges per node | 0 = ingest-only; 8 = low RAM; 16 = balanced; 32+ = high recall |
| `ef_construct` | 100 | Search width during index build | Higher = better index quality, slower build |
| `ef` | 128 | Search width at query time | Higher = better recall, higher latency |
| `full_scan_threshold` | 10000 | Min segment size for HNSW | Below this, brute-force is used |

```python
from qdrant_client.models import HnswConfigDiff

client.create_collection(
    collection_name="high_recall",
    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
    hnsw_config=HnswConfigDiff(
        m=32,
        ef_construct=200,
    ),
)

# Override ef at search time
results = client.query_points(
    collection_name="high_recall",
    query=query_vector,
    search_params=SearchParams(hnsw_ef=256),
    limit=10,
)
```

### Search Parameters and Tuning

| Goal | `m` | `ef_construct` | `ef` (search) | Expected Recall |
|------|-----|----------------|---------------|-----------------|
| Fast ingestion, low RAM | 8 | 64 | 64 | ~90-95% |
| Balanced (default) | 16 | 100 | 128 | ~95-98% |
| High recall | 32 | 200 | 256 | ~98-99% |
| Maximum recall | 64 | 512 | 512 | ~99%+ |

### Recall vs. Latency Tradeoffs

- Increasing `ef` improves recall but increases query latency linearly
- Increasing `m` improves recall and adds RAM overhead per vector
- At very high `ef` values, returns diminish — beyond ~512, you approach brute-force cost
- Use `exact: true` when you need 100% recall and the collection is small enough

### Payload Indexes

Always index fields used in filters:

```python
# Create indexes for common filter fields
client.create_payload_index("articles", "category", "keyword")
client.create_payload_index("articles", "published", "datetime")
client.create_payload_index("articles", "rating", "float")
client.create_payload_index("articles", "location", "geo")
client.create_payload_index("articles", "content", "text")  # full-text
```

### Filtering Behavior

Qdrant evaluates filters **during** HNSW traversal (not as a post-filter step) thanks to filterable HNSW. This means:

- Filters do not degrade search quality for well-indexed fields
- Unindexed fields require full payload loading — always create indexes
- Highly selective filters (matching <1% of points) may trigger a brute-force scan on matching points, which can be faster than graph traversal

### Hybrid Retrieval

The **Universal Query API** (`/collections/{name}/points/query`) supports multi-stage retrieval:

```python
# Stage 1: Prefetch from dense and sparse
# Stage 2: Fuse results with RRF
results = client.query_points(
    collection_name="hybrid_docs",
    prefetch=[
        Prefetch(query=[0.1, 0.2, ...], using="dense", limit=50),
        Prefetch(
            query=SparseVector(indices=[5, 100, 250], values=[0.8, 0.5, 0.3]),
            using="sparse",
            limit=50,
        ),
    ],
    query=FusionQuery(fusion=Fusion.RRF),
    limit=10,
)
```

### Performance Considerations

- **Segment count**: Fewer segments = faster search. The optimizer merges segments in the background.
- **Payload index absence**: The #1 cause of slow filtered searches.
- **On-disk vectors**: 10-100x slower than in-memory for random access. Use mmap as a middle ground.
- **Quantization**: Reduces memory 4-32x with minimal recall loss when configured correctly.

---

## 9. API and SDK Usage

### REST API

Base URL: `http://localhost:6333`

#### Key Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/collections` | GET | List all collections |
| `/collections/{name}` | PUT | Create collection |
| `/collections/{name}` | GET | Get collection info |
| `/collections/{name}` | DELETE | Delete collection |
| `/collections/{name}/points` | PUT | Upsert points |
| `/collections/{name}/points/query` | POST | Universal Query (search, recommend, discover) |
| `/collections/{name}/points/scroll` | POST | Paginate through points |
| `/collections/{name}/points/count` | POST | Count points |
| `/collections/{name}/points/delete` | POST | Delete points |
| `/collections/{name}/points/payload` | PUT | Set payload |
| `/collections/{name}/index` | PUT | Create payload index |
| `/collections/{name}/snapshots` | POST | Create snapshot |
| `/metrics` | GET | Prometheus metrics |

#### REST Examples

```bash
# Create collection
curl -X PUT http://localhost:6333/collections/test \
  -H 'Content-Type: application/json' \
  -d '{
    "vectors": { "size": 4, "distance": "Cosine" }
  }'

# Upsert points
curl -X PUT http://localhost:6333/collections/test/points \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"id": 1, "vector": [0.05, 0.61, 0.76, 0.74], "payload": {"city": "Berlin"}},
      {"id": 2, "vector": [0.19, 0.81, 0.75, 0.11], "payload": {"city": "London"}}
    ]
  }'

# Search
curl -X POST http://localhost:6333/collections/test/points/query \
  -H 'Content-Type: application/json' \
  -d '{
    "query": [0.2, 0.1, 0.9, 0.7],
    "limit": 3,
    "with_payload": true
  }'

# Filtered search
curl -X POST http://localhost:6333/collections/test/points/query \
  -H 'Content-Type: application/json' \
  -d '{
    "query": [0.2, 0.1, 0.9, 0.7],
    "filter": {
      "must": [{"key": "city", "match": {"value": "London"}}]
    },
    "limit": 3,
    "with_payload": true
  }'

# Delete points
curl -X POST http://localhost:6333/collections/test/points/delete \
  -H 'Content-Type: application/json' \
  -d '{"points": [1, 2]}'
```

### gRPC API

Port 6334. Protocol Buffers (proto3) definitions. Higher throughput than REST for production workloads.

```python
# Python client with gRPC
client = QdrantClient(host="localhost", grpc_port=6334, prefer_grpc=True)
```

### Python SDK (`qdrant-client`)

```bash
pip install qdrant-client
```

#### Connect

```python
from qdrant_client import QdrantClient

# Remote server (REST)
client = QdrantClient(host="localhost", port=6333)

# Remote server (gRPC, recommended for production)
client = QdrantClient(host="localhost", grpc_port=6334, prefer_grpc=True)

# Qdrant Cloud
client = QdrantClient(url="https://xyz.cloud.qdrant.io", api_key="your-key")

# In-memory (no server needed, for testing)
client = QdrantClient(":memory:")

# Local persistent storage (no server needed)
client = QdrantClient(path="./local_qdrant")

# Async client
from qdrant_client import AsyncQdrantClient
async_client = AsyncQdrantClient(host="localhost", port=6333)
```

#### Create Collection

```python
from qdrant_client.models import Distance, VectorParams

client.create_collection(
    collection_name="my_collection",
    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
)
```

#### Upsert Points

```python
from qdrant_client.models import PointStruct

# Single or batch upsert
client.upsert(
    collection_name="my_collection",
    wait=True,
    points=[
        PointStruct(id=1, vector=[0.1, 0.2, ...], payload={"key": "value"}),
        PointStruct(id=2, vector=[0.3, 0.4, ...], payload={"key": "other"}),
    ],
)
```

#### Search

```python
results = client.query_points(
    collection_name="my_collection",
    query=[0.1, 0.2, ...],
    limit=10,
    with_payload=True,
    with_vectors=False,
).points
```

#### Filter

```python
from qdrant_client.models import Filter, FieldCondition, MatchValue, Range

results = client.query_points(
    collection_name="my_collection",
    query=[0.1, 0.2, ...],
    query_filter=Filter(
        must=[
            FieldCondition(key="category", match=MatchValue(value="tech")),
            FieldCondition(key="rating", range=Range(gte=4.0)),
        ]
    ),
    limit=10,
).points
```

#### Delete

```python
# By IDs
client.delete(collection_name="my_collection", points_selector=[1, 2, 3])

# By filter
from qdrant_client.models import FilterSelector
client.delete(
    collection_name="my_collection",
    points_selector=FilterSelector(
        filter=Filter(must=[FieldCondition(key="status", match=MatchValue(value="expired"))])
    ),
)
```

#### Batch Operations

```python
# Batch upsert in chunks for large datasets
import itertools

def chunked(iterable, size):
    it = iter(iterable)
    while chunk := list(itertools.islice(it, size)):
        yield chunk

for batch in chunked(all_points, 500):
    client.upsert(collection_name="my_collection", wait=False, points=batch)
```

### JavaScript/TypeScript SDK (`@qdrant/js-client-rest`)

```bash
npm install @qdrant/js-client-rest
```

```typescript
import { QdrantClient } from '@qdrant/js-client-rest';

// Connect
const client = new QdrantClient({ host: 'localhost', port: 6333 });

// With API key
const cloudClient = new QdrantClient({
  url: 'https://xyz.cloud.qdrant.io',
  apiKey: 'your-key',
});

// Create collection
await client.createCollection('articles', {
  vectors: { size: 384, distance: 'Cosine' },
});

// Upsert points
await client.upsert('articles', {
  wait: true,
  points: [
    { id: 1, vector: [0.05, 0.61, 0.76, 0.74], payload: { city: 'Berlin' } },
    { id: 2, vector: [0.19, 0.81, 0.75, 0.11], payload: { city: 'London' } },
  ],
});

// Search
const results = await client.query('articles', {
  query: [0.2, 0.1, 0.9, 0.7],
  limit: 3,
  with_payload: true,
});

// Filtered search
const filtered = await client.query('articles', {
  query: [0.2, 0.1, 0.9, 0.7],
  filter: {
    must: [{ key: 'city', match: { value: 'London' } }],
  },
  limit: 3,
});

// Delete
await client.delete('articles', { points: [1, 2] });
```

### Other SDKs

| SDK | Install | Notes |
|-----|---------|-------|
| **Rust** | `cargo add qdrant-client` | gRPC-based, native performance |
| **Go** | `go get github.com/qdrant/go-client` | gRPC-based |
| **Java** | Maven: `io.qdrant:client` | gRPC-based |
| **.NET** | `dotnet add package Qdrant.Client` | gRPC-based |

All non-Python/JS SDKs use gRPC. The API surface is equivalent across all SDKs.

---

## 10. Advanced Features

### Hybrid Search

Hybrid search combines dense (semantic) and sparse (keyword) retrieval for better overall recall. Qdrant supports this natively via the Universal Query API.

**Setup: Collection with both dense and sparse vectors**

```python
from qdrant_client.models import SparseVectorParams

client.create_collection(
    collection_name="hybrid",
    vectors_config={
        "dense": VectorParams(size=768, distance=Distance.COSINE),
    },
    sparse_vectors_config={
        "sparse": SparseVectorParams(),
    },
)
```

**Insert with both vector types:**

```python
from qdrant_client.models import SparseVector

client.upsert(
    collection_name="hybrid",
    points=[
        PointStruct(
            id=1,
            vector={
                "dense": [0.1, 0.2, ...],
                "sparse": SparseVector(indices=[10, 50, 100], values=[0.8, 0.5, 0.3]),
            },
            payload={"title": "My Document"},
        ),
    ],
)
```

**Hybrid query with RRF fusion:**

```python
results = client.query_points(
    collection_name="hybrid",
    prefetch=[
        Prefetch(query=[0.1, 0.2, ...], using="dense", limit=50),
        Prefetch(
            query=SparseVector(indices=[10, 50], values=[0.9, 0.4]),
            using="sparse",
            limit=50,
        ),
    ],
    query=FusionQuery(fusion=Fusion.RRF),
    limit=10,
)
```

### Quantization

Reduce memory and increase speed at the cost of some precision.

```python
from qdrant_client.models import ScalarQuantization, ScalarQuantizationConfig, ScalarType

# Scalar quantization (4x memory reduction)
client.create_collection(
    collection_name="quantized",
    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
    quantization_config=ScalarQuantization(
        scalar=ScalarQuantizationConfig(
            type=ScalarType.INT8,
            quantile=0.99,
            always_ram=True,  # Keep quantized vectors in RAM
        ),
    ),
)

# Search with oversampling for better recall
results = client.query_points(
    collection_name="quantized",
    query=query_vector,
    search_params=SearchParams(
        quantization=QuantizationSearchParams(
            rescore=True,       # Rescore top candidates with original vectors
            oversampling=2.0,   # Retrieve 2x candidates, then rescore
        ),
    ),
    limit=10,
)
```

**Quantization comparison:**

| Method | Compression | RAM Savings | Speed Gain | Recall Impact | Best For |
|--------|-------------|-------------|------------|---------------|----------|
| Scalar (int8) | 4x | ~75% | ~2x | Minimal (<1%) | General use |
| Binary (1-bit) | 32x | ~97% | ~40x | Noticeable | dims >= 1024 |
| Product | Configurable | Variable | Moderate | Moderate | Very large datasets |
| 1.5-bit | ~21x | ~95% | ~20x | Moderate | Between binary and scalar |
| 2-bit | ~16x | ~94% | ~15x | Slight | Better recall than 1.5-bit |

### Distributed Mode

Enable clustering with Raft consensus:

```bash
# Node 1 (bootstrap)
./qdrant --uri http://node1:6335

# Node 2 (join cluster)
./qdrant --uri http://node2:6335 --bootstrap http://node1:6335

# Node 3 (join cluster)
./qdrant --uri http://node3:6335 --bootstrap http://node1:6335
```

### Replication

```python
client.create_collection(
    collection_name="replicated",
    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
    replication_factor=3,
    write_consistency_factor=2,  # Majority writes
)
```

**Write consistency levels:**
- `weak` — Any peer processes the write (fastest)
- `medium` — Leader preferred
- `strong` — Leader required with Raft consensus

### Sharding

```python
# Automatic (default)
client.create_collection(
    collection_name="auto_sharded",
    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
    shard_number=6,  # Number of shards
)

# Custom sharding
client.create_collection(
    collection_name="custom_sharded",
    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
    sharding_method=ShardingMethod.CUSTOM,
)
```

### Snapshots

```python
# Create collection snapshot
snapshot = client.create_snapshot("my_collection")

# List snapshots
snapshots = client.list_snapshots("my_collection")

# Full snapshot (all collections + aliases)
full = client.create_full_snapshot()

# Restore from URL
# POST /collections/my_collection/snapshots/recover
# {"location": "https://storage.example.com/snapshot.snapshot"}
```

### Aliases

Zero-downtime collection swaps:

```python
# Create alias
client.update_collection_aliases(
    change_aliases_operations=[
        CreateAliasOperation(
            create_alias=CreateAlias(
                collection_name="articles_v2",
                alias_name="articles",
            )
        ),
    ]
)

# Swap alias atomically (blue-green)
client.update_collection_aliases(
    change_aliases_operations=[
        DeleteAliasOperation(delete_alias=DeleteAlias(alias_name="articles")),
        CreateAliasOperation(
            create_alias=CreateAlias(
                collection_name="articles_v3",
                alias_name="articles",
            )
        ),
    ]
)
```

### Tenant Isolation

Combine payload-based tenancy with JWT RBAC:

1. Add `tenant_id` to every point
2. Create a keyword index with `is_tenant=True`
3. Issue JWT tokens scoped to specific `tenant_id` values
4. Qdrant enforces the tenant filter server-side, preventing cross-tenant access

### Large-Scale Ingestion

```python
# Optimal ingestion pattern
import itertools

# 1. Create collection with optimized settings for bulk load
client.create_collection(
    collection_name="bulk",
    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
    optimizers_config=OptimizersConfigDiff(
        indexing_threshold=0,  # Disable indexing during bulk load
    ),
)

# 2. Upsert in parallel batches of 500-1000
for batch in chunked(all_points, 500):
    client.upsert(collection_name="bulk", wait=False, points=batch)

# 3. Re-enable indexing after bulk load
client.update_collection(
    collection_name="bulk",
    optimizers_config=OptimizersConfigDiff(
        indexing_threshold=20000,  # Restore default
    ),
)
```

### RAG-Specific Design Patterns

See [Section 12](#12-qdrant-for-rag-and-ai-systems) for dedicated RAG coverage.

---

## 11. Production Deployment Guidance

### High Availability

1. Deploy **3+ nodes** minimum
2. Set `replication_factor >= 2` (ideally 3)
3. Set `write_consistency_factor = (replication_factor / 2) + 1` for strong consistency
4. Use Kubernetes **StatefulSets** with **pod anti-affinity** rules
5. Place replicas in **different availability zones**
6. Use a **load balancer** in front of Qdrant nodes for client connections

### Scaling Patterns

| Pattern | When | How |
|---------|------|-----|
| **Vertical** | Single-node performance sufficient | Add more RAM, faster NVMe |
| **Horizontal (sharding)** | Data exceeds single-node capacity | Add nodes, increase `shard_number` |
| **Read scaling** | High query load | Increase `replication_factor` |
| **Write scaling** | High ingestion load | More shards across more nodes |

### Resource Sizing

| Scale | RAM (in-memory) | RAM (mmap) | RAM (scalar quantized) |
|-------|-----------------|------------|------------------------|
| 1M vectors, 768 dims | ~4.3 GB | ~135 MB | ~1.1 GB |
| 10M vectors, 768 dims | ~43 GB | ~1.35 GB | ~11 GB |
| 100M vectors, 768 dims | ~430 GB | ~13.5 GB | ~110 GB |

**Formula (full in-memory, no quantization):**
```
RAM ≈ num_vectors × dimensions × 4 bytes × 1.5
```
The 1.5x multiplier accounts for indexes, metadata, and temporary segments.

**Disk:** Plan for 2-3x the raw vector data size to account for indexes, WAL, and snapshots.

### Storage Considerations

- **NVMe SSDs** are strongly recommended for mmap and on-disk modes
- SATA SSDs work but with higher tail latencies
- HDD is not recommended for any workload
- Plan snapshot storage separately (can be large)

### Backup and Recovery

1. **Scheduled snapshots**: Automate via cron + API call
2. **Upload to object storage**: Download snapshots, upload to S3/GCS/Azure Blob
3. **Full snapshots**: Include all collections + aliases in one file
4. **Test restores regularly**: Verify snapshot integrity by restoring to a staging instance
5. **Qdrant Cloud**: Automated backups with configurable retention

```bash
# Automated snapshot script
#!/bin/bash
COLLECTION="my_collection"
QDRANT_URL="http://localhost:6333"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Create snapshot
SNAP=$(curl -s -X POST "$QDRANT_URL/collections/$COLLECTION/snapshots" | jq -r '.result.name')

# Download snapshot
curl -o "backup_${COLLECTION}_${TIMESTAMP}.snapshot" \
  "$QDRANT_URL/collections/$COLLECTION/snapshots/$SNAP"

# Upload to S3
aws s3 cp "backup_${COLLECTION}_${TIMESTAMP}.snapshot" s3://my-backups/qdrant/
```

### Security Hardening

1. **Enable API key authentication** — never run Qdrant without auth in production
2. **Enable TLS** — especially when API keys are in use (prevents credential sniffing)
3. **Use read-only API keys** for services that only need search access
4. **Enable JWT RBAC** for multi-tenant or fine-grained access control
5. **Firewall ports** — restrict 6333/6334 to known clients; 6335 (internal) to cluster peers only
6. **Use reverse proxy** (nginx/Caddy) for additional TLS termination and rate limiting
7. **Rotate API keys** periodically

```yaml
# Production config.yaml
service:
  api_key: "${QDRANT_API_KEY}"
  read_only_api_key: "${QDRANT_READ_ONLY_KEY}"
  enable_tls: true
  jwt_rbac: true

tls:
  cert: /etc/qdrant/tls/cert.pem
  key: /etc/qdrant/tls/key.pem
```

### Monitoring and Alerting

**Prometheus metrics** available at `GET /metrics`:

| Metric Category | What to Monitor |
|-----------------|-----------------|
| Search latency | p50, p95, p99 — alert if p99 > your SLA |
| Upsert throughput | Points/second — detect ingestion bottlenecks |
| Collection size | Vector count — capacity planning |
| Memory usage | RSS, mmap usage — prevent OOM |
| Segment count | High counts indicate optimizer backlog |
| Error rates | HTTP 5xx, gRPC errors |

**Grafana dashboard setup:**
1. Configure Prometheus to scrape `/metrics` from each Qdrant node
2. Import or build dashboards for the key metrics above
3. Set alerts for latency spikes, error rate increases, and memory pressure

### Upgrade Strategy

1. **Create full snapshot** before upgrading
2. **Rolling upgrade** in distributed deployments (one node at a time)
3. Qdrant maintains backward compatibility for stored data
4. Test upgrades on a staging cluster first
5. Helm: `helm upgrade qdrant qdrant/qdrant --version X.Y.Z`

### Disaster Recovery

- Maintain snapshot copies in a different region/cloud
- Document and test the full restore procedure
- For Qdrant Cloud: leverage built-in multi-region backups
- RTO depends on snapshot size and restoration speed (plan for 30min-2h for large datasets)

---

## 12. Qdrant for RAG and AI Systems

### Embedding Storage Patterns

| Pattern | Description | When to Use |
|---------|-------------|-------------|
| **One point per chunk** | Each text chunk is a separate point | Standard RAG, most common |
| **Named vectors** | Title + body + summary embeddings on same point | Multi-view retrieval |
| **Sparse + dense** | SPLADE/BM25 sparse + semantic dense per chunk | Hybrid RAG |
| **Parent-child** | Chunk points with `parent_id` payload linking to parent doc | Hierarchical retrieval |

### Chunking Implications

Chunking strategy directly affects retrieval quality. Qdrant doesn't chunk for you — you must do it upstream.

| Chunk Size | Pros | Cons |
|------------|------|------|
| Small (100-200 tokens) | Precise retrieval, less noise | May lose context, more points to store |
| Medium (300-500 tokens) | Good balance of precision and context | Standard choice |
| Large (500-1000 tokens) | More context per result | May dilute relevance, fewer results fit in LLM context |
| Overlapping | Preserves boundary context | More storage, potential duplicates in results |

**Store chunk metadata in payloads:**

```python
PointStruct(
    id=uuid4(),
    vector=embedding,
    payload={
        "document_id": "doc-123",
        "chunk_index": 5,
        "text": "The actual chunk text...",
        "source": "user_manual.pdf",
        "page": 12,
        "section": "Installation",
        "created_at": "2025-06-01T00:00:00Z",
    },
)
```

### Metadata Filtering Patterns

| Use Case | Filter Pattern |
|----------|---------------|
| Scope to user's documents | `tenant_id == user_id` |
| Recent documents only | `created_at >= 30 days ago` |
| Specific source type | `source_type == "manual"` |
| Access control | `access_level in user.permissions` |
| Version filtering | `version == "latest"` |
| Language filtering | `language == "en"` |

### Hybrid Retrieval Strategies

**Strategy 1: Dense + Sparse fusion (recommended)**
```
Query → [Dense Embedding] + [Sparse Embedding (SPLADE/BM25)]
     → Qdrant prefetch (dense top-50, sparse top-50)
     → RRF fusion → top-10 results
     → LLM
```

**Strategy 2: Multi-stage with reranking**
```
Query → Dense search (top-50 from Qdrant)
     → Cross-encoder reranker (external, e.g., Cohere Rerank)
     → top-10 results
     → LLM
```

**Strategy 3: Dense + full-text**
```
Query → Dense search (semantic) + Full-text payload search (keyword)
     → Qdrant prefetch with RRF fusion
     → top-10 results
     → LLM
```

### Reranking Pipeline Placement

Qdrant performs first-stage retrieval (candidate generation). Reranking happens **after** Qdrant returns results and **before** sending context to the LLM:

```
User Query
    ↓
Embedding Model (query → vector)
    ↓
Qdrant Search (top-50 candidates)
    ↓
Reranker (cross-encoder, Cohere, etc.) → top-10
    ↓
LLM (generate answer with top-10 as context)
```

### Freshness and Update Strategies

| Strategy | Approach | Latency | Complexity |
|----------|----------|---------|------------|
| **Full rebuild** | Drop and recreate collection periodically | Minutes-hours | Low |
| **Incremental upsert** | Upsert new/changed points, delete removed ones | Seconds | Medium |
| **Alias swap** | Build new collection, atomically swap alias | Zero downtime | Medium |
| **TTL via payload** | Add `expires_at` field, periodically delete expired | Configurable | Low |

**Alias swap pattern (recommended for production):**

```python
# 1. Build new collection
client.create_collection("articles_v2", ...)
# ... populate with fresh data ...

# 2. Atomic swap
client.update_collection_aliases(
    change_aliases_operations=[
        DeleteAliasOperation(delete_alias=DeleteAlias(alias_name="articles")),
        CreateAliasOperation(create_alias=CreateAlias(
            collection_name="articles_v2",
            alias_name="articles",
        )),
    ]
)

# 3. Delete old collection
client.delete_collection("articles_v1")
```

### Example RAG Architecture

```
┌─────────────────────────────────────────────────────────┐
│  User Question                                          │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│  Embedding Model (e.g., text-embedding-3-small)         │
│  Query → Dense vector (1536 dims)                       │
│  Query → Sparse vector (SPLADE) [optional]              │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│  Qdrant                                                  │
│  ┌─────────────────────────────────────────────────────┐ │
│  │ Collection: "knowledge_base"                        │ │
│  │ Named vectors: dense (1536) + sparse               │ │
│  │ Payload: text, source, tenant_id, created_at       │ │
│  │ Payload indexes: tenant_id (keyword), created_at   │ │
│  │ Quantization: scalar (int8)                        │ │
│  └─────────────────────────────────────────────────────┘ │
│  Hybrid query: prefetch dense + sparse → RRF → top 50   │
│  Filter: tenant_id == current_user                       │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│  Reranker (Cohere Rerank / Cross-encoder)                │
│  top 50 → rerank → top 10                               │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│  LLM (Claude, GPT, etc.)                                 │
│  System: "Answer based on the following context..."      │
│  Context: [top 10 chunks with metadata]                  │
│  User: original question                                 │
└──────────────────────────────────────────────────────────┘
```

---

## 13. Performance Optimization

### Write Throughput Tuning

1. **Batch upserts**: 500-1000 points per call
2. **Parallel ingestion**: Use multiple threads/connections
3. **`wait=False`**: Fire-and-forget for throughput (confirm writes in a separate check)
4. **Disable indexing during bulk load**: Set `indexing_threshold=0`, re-enable after
5. **Increase WAL capacity**: Higher `wal_capacity_mb` reduces flush frequency

```python
# Bulk ingestion config
client.create_collection(
    collection_name="bulk_load",
    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
    optimizers_config=OptimizersConfigDiff(
        indexing_threshold=0,           # Disable HNSW during bulk insert
        memmap_threshold=20000,
        default_segment_number=4,
    ),
    wal_config=WalConfigDiff(
        wal_capacity_mb=64,
    ),
)
```

### Query Latency Tuning

1. **Use gRPC** instead of REST for lower overhead
2. **Tune `ef`** — start at 128, increase only if recall is insufficient
3. **Enable quantization** — scalar quantization typically gives 2x speedup with <1% recall loss
4. **Use `oversampling` + `rescore`** with quantization for best recall/speed balance
5. **Return only needed fields**: `with_payload=["title", "id"]` instead of `with_payload=True`
6. **Avoid `with_vectors=True`** unless you need the vectors back

### Memory and CPU Planning

| Component | Memory Impact |
|-----------|---------------|
| Vectors (in-memory) | `num_vectors × dims × 4 bytes` |
| HNSW index | ~`num_vectors × m × 8 bytes` (varies) |
| Payload indexes | Proportional to indexed field cardinality |
| Quantized vectors | Depends on method (4x to 32x smaller) |
| OS page cache (mmap) | Bounded by available RAM; frequently accessed pages cached |

**CPU considerations:**
- HNSW index build is CPU-intensive (parallelized across cores)
- Search is also CPU-bound for graph traversal
- Plan 2-4 CPU cores per shard for production workloads
- GPU-accelerated HNSW indexing available since v1.13 (up to 10x faster build)

### Index Tuning

| Scenario | `m` | `ef_construct` | Notes |
|----------|-----|----------------|-------|
| Write-heavy, read-light | 8 | 64 | Faster index build, acceptable recall |
| Read-heavy, write-light | 32 | 200 | Higher recall, slower build |
| Balanced | 16 | 100 | Default; good starting point |
| Ingest-only (no search) | 0 | N/A | `m=0` disables HNSW entirely |

### Filtering Optimization

1. **Always create payload indexes** on filtered fields
2. **Use `is_tenant=True`** for tenant fields to enable storage co-location
3. **Avoid unindexed full-text filters** — they force payload loading from disk
4. **Compound filters**: Place the most selective condition first in `must` arrays
5. **Nested filters**: Use explicit `nested` filter syntax; don't rely on dot-notation

### Batch Ingestion Best Practices

1. Set `indexing_threshold=0` before bulk load
2. Batch in groups of 500-1000 points
3. Use `wait=False` for throughput
4. Use parallel connections (4-8 threads)
5. Re-enable indexing after load completes
6. Wait for optimizer to finish building indexes (monitor via collection info)
7. Verify point count matches expectations

### Benchmarking Guidance

- Use Qdrant's built-in `/metrics` for latency percentiles
- Test with your actual data and query patterns
- Benchmark at expected production load (concurrent queries)
- Compare with and without quantization
- Test recall against a ground-truth exact search
- Key metrics: p99 latency, throughput (QPS), recall@10, memory usage

---

## 14. Common Pitfalls

### Bad Schema Decisions

| Pitfall | Impact | Fix |
|---------|--------|-----|
| One collection per tenant | Resource overhead, management burden | Use payload-based multitenancy |
| Missing payload indexes | 10-100x slower filtered searches | Always index filter fields before production load |
| Storing large text in payload | Bloated memory, slow payload loading | Store references; keep full text external |
| Inconsistent field types | Index errors, unexpected filter behavior | Enforce type consistency in your application layer |

### Wrong Metric Choice

- Using **Euclidean** for text embeddings (most models optimize for Cosine)
- Using **Cosine** when embeddings are already normalized (Dot Product is marginally faster)
- Not matching the metric to the embedding model's recommendation

> **Fix:** Check your embedding model's documentation for the recommended distance metric.

### Over-Filtering

- Adding many `must` conditions on unindexed fields
- Filtering down to <100 candidate vectors, causing the optimizer to switch to brute-force
- Using `values_count` or `is_empty` on unindexed fields

> **Fix:** Index all filter fields. If filters are extremely selective, consider whether a traditional database lookup + targeted Qdrant search is more efficient.

### Poor Chunking Strategy

- Chunks too small: Lose context, retrieve noise
- Chunks too large: Dilute relevance signal, waste LLM context window
- No overlap: Miss information at chunk boundaries
- No metadata: Cannot filter or trace chunks back to source

> **Fix:** Start with 300-500 token chunks, 50-100 token overlap. Store document_id, chunk_index, source, and section metadata.

### Incorrect Scale Assumptions

- Assuming mmap has the same performance as in-memory (it doesn't for random access patterns)
- Underestimating HNSW index memory overhead
- Not accounting for temporary segments during index builds (1.5x multiplier)
- Sizing RAM for vectors only, forgetting indexes and metadata

> **Fix:** Use the formula `RAM ≈ num_vectors × dims × 4 × 1.5` for in-memory. Add 30-50% headroom for production.

### Operational Blind Spots

- No monitoring of optimizer status (segments accumulating without merging)
- No automated snapshots (data loss risk)
- No TLS with API key auth (credential sniffing)
- Not testing snapshot restore procedures
- Running single-node in production without backups

---

## 15. Comparison and Tradeoffs

### When Qdrant Is a Strong Choice

- **Complex filtered search** — Filterable HNSW is Qdrant's standout feature
- **Hybrid search** — Native dense + sparse fusion with RRF/DBSF
- **Quantization variety** — Most quantization strategies of any open-source vector DB
- **Self-hosted control** — Full Apache 2.0, no vendor lock-in
- **Rust performance** — Memory safety without GC pauses
- **Multi-tenancy** — Dedicated tenant-aware storage optimization
- **Prototyping to production** — In-memory client for testing, clustered deployment for production

### When Another Solution May Be Better

| Need | Better Alternative | Why |
|------|--------------------|-----|
| Fully managed, zero-ops | **Pinecone** | No infrastructure to manage; easiest onboarding |
| Already using PostgreSQL | **pgvector** | No new infrastructure; SQL familiarity |
| Billions of vectors, raw throughput | **Milvus/Zilliz** | Designed for extreme scale from the ground up |
| Quick prototype, Python-only | **Chroma** | Simplest API, embedded by default |
| Heavy full-text search + vectors | **Elasticsearch/OpenSearch** | Mature full-text with vector bolt-on |
| Graph + vector queries | **Weaviate** | Native GraphQL, cross-references |

### Comparison Matrix

| Feature | Qdrant | Pinecone | Weaviate | Milvus | Chroma | pgvector |
|---------|--------|----------|----------|--------|--------|----------|
| **Open Source** | Yes (Apache 2.0) | No | Yes (BSD-3) | Yes (Apache 2.0) | Yes (Apache 2.0) | Yes (PostgreSQL) |
| **Language** | Rust | Proprietary | Go | Go/C++ | Python | C |
| **Self-hosted** | Yes | No | Yes | Yes | Yes | Yes |
| **Managed cloud** | Yes | Yes | Yes | Yes (Zilliz) | No | Various |
| **Hybrid search** | Native | Yes | Yes | Yes | Basic | No |
| **Filtering quality** | Excellent (filterable HNSW) | Good | Good | Good | Basic | SQL WHERE |
| **Sparse vectors** | Yes | Yes | Limited | Yes | No | No |
| **Quantization options** | 5 types | Limited | PQ, BQ | Various | No | halfvec only |
| **Named vectors** | Yes | Namespaces | Yes | Yes | No | No |
| **RBAC** | JWT-based | Yes | Yes | Yes | No | Postgres roles |
| **Practical max scale** | Billions | Billions | Billions | Billions | Millions | ~100M |
| **Setup complexity** | Low | Lowest | Medium | High | Lowest | Low (if Postgres exists) |

### Tradeoffs vs. Traditional Databases

- **No ACID transactions** — Qdrant provides eventual consistency in distributed mode
- **No SQL** — Query language is REST/gRPC-based, not SQL
- **No joins** — Relationships must be modeled differently (payloads, application-level joins)
- **Specialized workload** — Qdrant excels at similarity search but isn't a general-purpose database

---

## 16. Best Practices Checklist

- [ ] **Choose the correct distance metric** for your embedding model (usually Cosine)
- [ ] **Create payload indexes** on every field you filter on — before loading data
- [ ] **Use payload-based multitenancy** instead of multiple collections
- [ ] **Enable API key authentication** and TLS in production
- [ ] **Batch upserts** at 500-1000 points per call for ingestion
- [ ] **Disable indexing during bulk loads** (`indexing_threshold=0`), re-enable after
- [ ] **Enable scalar quantization** for 4x memory reduction with minimal recall loss
- [ ] **Use `oversampling` + `rescore`** with quantization for best recall
- [ ] **Use gRPC** (`prefer_grpc=True`) for production Python clients
- [ ] **Size RAM correctly**: `num_vectors × dims × 4 bytes × 1.5` for in-memory
- [ ] **Set up automated snapshots** and test restore procedures
- [ ] **Monitor Prometheus metrics**: search latency, memory, segment counts
- [ ] **Use collection aliases** for zero-downtime updates
- [ ] **Deploy 3+ nodes** with `replication_factor >= 2` for HA
- [ ] **Use NVMe SSDs** for mmap and on-disk storage modes
- [ ] **Start with medium chunk sizes** (300-500 tokens) for RAG
- [ ] **Store chunk metadata** (document_id, source, section) in payloads
- [ ] **Use hybrid search** (dense + sparse) for best RAG retrieval quality
- [ ] **Add a reranker** between Qdrant retrieval and LLM context injection
- [ ] **Never run Qdrant without auth** in any network-accessible deployment

---

## 17. Quick Start Recap

```bash
# 1. Start Qdrant
docker run -p 6333:6333 -p 6334:6334 \
    -v "$(pwd)/qdrant_storage:/qdrant/storage:z" \
    qdrant/qdrant

# 2. Install Python client
pip install qdrant-client
```

```python
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

# 3. Connect
client = QdrantClient(host="localhost", port=6333)

# 4. Create collection
client.create_collection(
    collection_name="my_docs",
    vectors_config=VectorParams(size=384, distance=Distance.COSINE),
)

# 5. Create payload index
client.create_payload_index("my_docs", "category", "keyword")

# 6. Insert data
client.upsert(
    collection_name="my_docs",
    points=[
        PointStruct(id=1, vector=[0.1, 0.2, ...], payload={"category": "tech", "text": "..."}),
        PointStruct(id=2, vector=[0.3, 0.4, ...], payload={"category": "science", "text": "..."}),
    ],
)

# 7. Search
results = client.query_points(
    collection_name="my_docs",
    query=[0.2, 0.1, ...],
    limit=5,
    with_payload=True,
).points

# 8. Filtered search
from qdrant_client.models import Filter, FieldCondition, MatchValue

results = client.query_points(
    collection_name="my_docs",
    query=[0.2, 0.1, ...],
    query_filter=Filter(must=[
        FieldCondition(key="category", match=MatchValue(value="tech")),
    ]),
    limit=5,
).points
```

---

## 18. References

### Official Documentation

- [Qdrant Documentation](https://qdrant.tech/documentation/) — Comprehensive official docs
- [Qdrant API Reference](https://api.qdrant.tech/api-reference) — OpenAPI specification
- [Qdrant Quickstart](https://qdrant.tech/documentation/quickstart/) — Getting started guide
- [Python Client Docs](https://python-client.qdrant.tech/) — Python SDK reference

### Repositories

- [Qdrant Core](https://github.com/qdrant/qdrant) — Main Rust repository
- [Qdrant Python Client](https://github.com/qdrant/qdrant-client) — Official Python SDK
- [Qdrant JS Client](https://github.com/qdrant/qdrant-js) — Official TypeScript SDK
- [Qdrant Helm Chart](https://github.com/qdrant/qdrant-helm) — Kubernetes deployment
- [FastEmbed](https://github.com/qdrant/fastembed) — Lightweight embedding library by Qdrant

### Key Technical Resources

- [Filtering in Qdrant](https://qdrant.tech/documentation/search/filtering/) — Filter syntax reference
- [Distributed Deployment](https://qdrant.tech/documentation/operations/distributed_deployment/) — Clustering guide
- [Security Configuration](https://qdrant.tech/documentation/operations/security/) — Auth, TLS, RBAC
- [Scalar Quantization](https://qdrant.tech/articles/scalar-quantization/) — Quantization deep dive
- [Binary Quantization](https://qdrant.tech/articles/binary-quantization/) — 40x speed improvement guide
- [Hybrid Search with Query API](https://qdrant.tech/articles/hybrid-search/) — Hybrid retrieval guide
- [Multitenancy Patterns](https://qdrant.tech/articles/multitenancy/) — Tenant isolation approaches
- [Memory Consumption](https://qdrant.tech/articles/memory-consumption/) — RAM sizing guide
- [Database Optimization FAQ](https://qdrant.tech/documentation/faq/database-optimization/) — Performance FAQ
- [Qdrant 2025 Recap](https://qdrant.tech/blog/2025-recap/) — Recent feature additions

---

## Appendix A: Feature Summary

| Category | Features |
|----------|----------|
| **Vector Types** | Dense, sparse, named vectors, multivector |
| **Distance Metrics** | Cosine, Dot Product, Euclidean, Manhattan |
| **Index** | HNSW (filterable), payload indexes (keyword, integer, float, geo, text, datetime, bool, uuid) |
| **Search Modes** | ANN, exact, hybrid (dense+sparse), recommend, discover, grouped |
| **Fusion** | RRF, DBSF |
| **Quantization** | Scalar (int8), Product, Binary, 1.5-bit, 2-bit |
| **Storage** | In-memory, mmap, on-disk; WAL for durability |
| **Distributed** | Raft consensus, sharding (auto/custom), replication, write consistency levels |
| **Security** | API keys, read-only keys, JWT RBAC, TLS, mutual TLS |
| **Operations** | Snapshots, aliases, Prometheus metrics, web dashboard |
| **SDKs** | Python, TypeScript, Rust, Go, Java, .NET |
| **Integrations** | LangChain, LlamaIndex, Haystack, Semantic Kernel, OpenAI, Cohere, FastEmbed |

## Appendix B: Recommended Learning Path

1. **Day 1**: Run Qdrant via Docker. Complete the [Quick Start Recap](#17-quick-start-recap). Create a collection, insert points, run a search.
2. **Day 2**: Add payload indexes and filtered search. Experiment with different filter conditions (match, range, boolean logic).
3. **Day 3**: Try named vectors and hybrid search (dense + sparse). Set up a simple RAG pipeline with LangChain or LlamaIndex.
4. **Day 4**: Enable scalar quantization. Benchmark recall and latency with and without it. Experiment with `oversampling` and `rescore`.
5. **Day 5**: Explore multitenancy patterns. Add a `tenant_id` payload index with `is_tenant=True`. Practice tenant-scoped queries.
6. **Week 2**: Set up a 3-node cluster. Configure replication. Practice snapshot/restore. Enable API key auth and TLS.
7. **Week 3**: Integrate with your embedding model of choice. Build a production RAG pipeline with hybrid search and reranking.
8. **Ongoing**: Monitor metrics, tune HNSW parameters, benchmark with production data, iterate on chunking strategy.

## Appendix C: Top 10 Implementation Tips

1. **Always create payload indexes before loading data** — retrofitting indexes on a large collection is slow.
2. **Use `is_tenant=True`** on tenant fields — it triggers storage co-location that significantly speeds up tenant-scoped queries.
3. **Start with scalar quantization** — it's the safest quantization method (4x memory savings, <1% recall loss) and easy to enable.
4. **Batch upserts at 500-1000 points** — smaller batches add overhead; larger ones risk timeouts.
5. **Use `wait=False` for bulk ingestion, `wait=True` for user-facing writes** — balance throughput vs. confirmation.
6. **Prefer gRPC over REST for production** — lower overhead, higher throughput, especially under concurrent load.
7. **Don't return vectors in search results** — set `with_vectors=False` (default) to reduce response size and latency.
8. **Use collection aliases for zero-downtime updates** — swap aliases atomically instead of modifying collections in-place.
9. **Monitor optimizer status** — if segments accumulate and don't merge, your system may be under-resourced.
10. **Test your actual recall** — benchmark search results against brute-force exact search to know your real recall rate before going to production.

---

*Guide version: 1.0 — March 2026*
*Covers Qdrant v1.17.x*
*Sources: Official Qdrant documentation, GitHub repository, API reference, and technical articles.*

# Research Report: Qdrant Vector Database -- Comprehensive Guide

## Metadata
- **Date**: 2026-03-31
- **Research ID**: QDRANT-2026-001
- **Domain**: Technical (Vector Databases / AI Infrastructure)
- **Status**: Complete
- **Confidence**: High
- **Sources Consulted**: 30+

## Executive Summary

Qdrant is a high-performance, open-source vector similarity search engine written in Rust, currently at version 1.17.1 (March 2026). Licensed under Apache 2.0, it is developed by Qdrant Solutions GmbH (Berlin, Germany). Qdrant provides advanced ANN search via HNSW indexing, rich payload filtering, hybrid search (dense + sparse vectors), multiple quantization strategies, distributed deployment with Raft consensus, and comprehensive SDK support across 6+ languages. It is one of the leading vector databases for RAG, semantic search, and recommendation workloads.

## Research Question

Comprehensive analysis of Qdrant vector database covering architecture, features, deployment, APIs, performance tuning, security, production guidance, integrations, use cases, comparisons, and limitations.

---

## 1. What Qdrant Is

Qdrant (pronounced "quadrant") is an open-source vector similarity search engine and vector database purpose-built for AI applications [1][2].

| Attribute | Value |
|-----------|-------|
| **Language** | Rust (87.4%), Python (11.4%) |
| **Current Version** | v1.17.1 (released March 27, 2026) |
| **License** | Apache License 2.0 |
| **Company** | Qdrant Solutions GmbH (Berlin, Germany) |
| **GitHub Stars** | ~29,900 |
| **GitHub Forks** | ~2,200 |
| **Total Releases** | 111+ |

Key value proposition: Qdrant enables storage, searching, and management of high-dimensional vectors with attached metadata (payloads) and supports extended filtering capabilities. It is designed for production-grade, low-latency similarity search at scale [1][2].

---

## 2. Architecture

### 2.1 Core Hierarchy

```
Qdrant Instance
  └── TableOfContent (TOC) -- storage orchestrator
       └── Collection -- named set of points sharing vector config
            └── Shard -- independent store of points (via consistent hashing)
                 └── Segment -- stores data structures for a subset of points
                      ├── Vector Index (HNSW graph)
                      ├── Payload Index
                      ├── ID Mapper
                      └── Vector Storage (RAM / mmap / on-disk)
```

**Collections**: A collection is a named set of points that all share the same vector configuration (dimensionality and distance metric). Collections are managed by the TableOfContent (TOC) storage orchestrator [3][4].

**Points**: The fundamental data unit. Each point consists of:
- A unique ID (integer or UUID)
- One or more vectors (dense, sparse, or multivector)
- An optional payload (JSON-like metadata)

**Shards**: Collections are horizontally partitioned into shards using consistent hashing. Each shard is an independent store capable of performing all collection operations. Sharding can be automatic (default) or user-defined (since v1.7.0) [5].

**Segments**: Each shard stores data across multiple segments. A segment holds all data structures for a subset of points (vector storage, HNSW index, payload index, ID mapper). More segments mean faster indexing but potentially lower search performance since queries scan more segments [3][4].

### 2.2 HNSW Index

Qdrant uses the Hierarchical Navigable Small World (HNSW) graph-based index for approximate nearest neighbor (ANN) search [4][6].

**How HNSW works in Qdrant:**
1. Multi-layered graph structure with decreasing connectivity
2. Search starts at an entry point in the top (coarsest) layer
3. Navigates downward through layers, progressively refining
4. At each layer, explores nearest neighbors to find the best path
5. Final results come from the bottom (most connected) layer

**Key HNSW parameters:**

| Parameter | Purpose | Default | Guidance |
|-----------|---------|---------|----------|
| `m` | Max edges per node in graph | 16 | 0 = ingest-only, 8 = low RAM, 16 = balanced, 32 = high recall |
| `ef_construct` | Search width during index building | 100 | Higher = better quality index, slower build |
| `ef` | Search width at query time | 128 | Higher = better recall, slower search |
| `full_scan_threshold` | Min segment size for HNSW (below = brute force) | 10000 | Adjust based on segment sizes |

**Filterable HNSW**: Qdrant extends the standard HNSW graph with additional edges corresponding to indexed payload values. This enables high-quality filtered search without runtime overhead -- a key differentiator from other HNSW implementations [4].

**Recent improvements (2025-2026):**
- GPU-accelerated HNSW indexing (up to 10x faster ingestion) [7]
- Incremental HNSW indexing for upsert-heavy workloads [7]
- HNSW graph compression to reduce memory footprint [7]

### 2.3 Storage Model

Qdrant offers three storage modes for vectors:

| Mode | Description | Use Case |
|------|-------------|----------|
| **In-Memory** | Vectors stored entirely in RAM | Maximum performance, sufficient RAM available |
| **Memmap (mmap)** | Memory-mapped files; OS manages caching | Large datasets; frequently accessed vectors cached in RAM |
| **On-Disk** | Vectors stored on disk, read on demand | Memory-constrained environments, very large datasets |

The `memmap_threshold` configuration controls when Qdrant switches from in-memory to mmap storage for segments.

### 2.4 Write-Ahead Log (WAL)

Qdrant uses a Write-Ahead Log to ensure data persistence. All write operations are first recorded in the WAL before being applied to segments. This guarantees durability even in case of crashes. WAL configuration includes:
- `wal_capacity_mb` -- Maximum WAL size before rotation
- `wal_segments_ahead` -- Number of WAL segments to keep ahead

### 2.5 Optimizers

Qdrant runs background optimizer processes that:
- Merge small segments into larger ones
- Build/rebuild HNSW indexes on segments
- Apply vacuum (remove deleted points)
- Balance segment sizes

Optimizer configuration parameters:
- `deleted_threshold` -- Proportion of deleted vectors before vacuum triggers
- `vacuum_min_vector_number` -- Minimum vectors in segment to trigger vacuum
- `default_segment_number` -- Target number of segments
- `max_segment_size` -- Maximum segment size in KiB
- `memmap_threshold` -- Threshold to switch segment to mmap storage
- `indexing_threshold` -- Minimum vectors before building HNSW index
- `flush_interval_sec` -- How often to flush data to disk

---

## 3. Features

### 3.1 ANN Search (HNSW)

Core search capability using HNSW algorithm (details in Section 2.2). Supports:
- Nearest neighbor queries with configurable `limit` and `offset`
- Exact search mode (brute-force, via `exact: true` parameter)
- Score threshold filtering
- With/without payload and vector return

### 3.2 Filtering

Qdrant provides rich payload filtering with boolean logic [8]:

**Boolean operators:**
- `must` -- All conditions must match (AND)
- `should` -- At least one condition must match (OR)
- `must_not` -- No conditions must match (NOT)
- `min_should` -- Minimum number of should conditions to match

**Filter condition types:**

| Condition | Description | Example |
|-----------|-------------|---------|
| `match` | Exact value match | `FieldCondition(key="city", match=MatchValue(value="London"))` |
| `range` | Numeric/datetime range | `Range(gte=10, lte=100)` |
| `geo_bounding_box` | Geographic bounding box | Lat/lon corners |
| `geo_radius` | Geographic radius | Center point + radius |
| `geo_polygon` | Geographic polygon | List of lat/lon points |
| `values_count` | Count of values in array field | `ValuesCount(gte=2)` |
| `is_empty` | Check if field is empty | `IsEmpty(key="field")` |
| `is_null` | Check if field is null | `IsNull(key="field")` |
| `has_id` | Match specific point IDs | List of IDs |
| `nested` | Filter on nested objects | Nested path + filter |
| `full_text_match` | Full-text search on text fields | `FieldCondition(key="text", match=MatchText(text="query"))` |
| `datetime_range` | Datetime range filtering | `Range(gte="2024-01-01T00:00:00Z")` |

### 3.3 Payload Indexing

Payload indexes dramatically speed up filtered searches. Without an index, Qdrant must load entire payloads from disk to check conditions [9].

| Index Type | Data Type | Use Case |
|------------|-----------|----------|
| `keyword` | String | Exact match on categorical values |
| `integer` | Integer | Range and exact match on integers |
| `float` | Float | Range queries on floating point |
| `geo` | Geo point | Geographic queries |
| `text` | Text/String | Full-text search (tokenized) |
| `datetime` | Datetime | Temporal range queries |
| `bool` | Boolean | Boolean match |
| `uuid` | UUID | UUID exact match |

Multitenancy-optimized indexing: Mark tenant fields with `is_tenant=True` to co-locate vectors of the same tenant for better performance [10].

### 3.4 Hybrid Search

Hybrid search combines dense and sparse vector search for better retrieval quality [11][12].

**Fusion methods:**
- **RRF (Reciprocal Rank Fusion)**: Combines results based on ranking positions only (ignores scores). Robust when score scales differ between methods.
- **DBSF (Distribution-Based Score Fusion)**: Takes actual scores into account, normalizing them based on their distributions.

**Prefetch mechanism**: Allows multi-stage search pipelines in a single API call:
1. Prefetch candidates with dense vectors
2. Rerank with sparse vectors (or vice versa)
3. Apply final scoring/fusion

**Universal Query API** (introduced in v1.10): Consolidates search, recommend, discover, and hybrid queries into a single endpoint (`/query`) with support for nested multistage queries [12].

### 3.5 Sparse Vector Support

Sparse vectors enable keyword/lexical search capabilities within Qdrant [11]:
- Support for SPLADE and BM25-style sparse representations
- Each sparse vector stores only non-zero dimensions with their indices and values
- Can be combined with dense vectors in the same collection via named vectors
- Ideal for exact-match and keyword-based retrieval scenarios

### 3.6 Named Vectors

Named vectors allow multiple vector spaces per point [13]:
- Define separate named vector spaces during collection creation
- Each named vector can have different dimensionality and distance metric
- Enables storing different embedding types (e.g., title embedding + body embedding + image embedding) on the same point
- Each named vector space has independent HNSW and quantization configuration

```python
# Example: Multiple named vectors
client.create_collection(
    collection_name="multi_vec",
    vectors_config={
        "title": VectorParams(size=384, distance=Distance.COSINE),
        "body": VectorParams(size=768, distance=Distance.COSINE),
        "image": VectorParams(size=512, distance=Distance.COSINE),
    }
)
```

### 3.7 Multitenancy

Two approaches [10]:

1. **Payload-based multitenancy** (recommended for many small tenants):
   - Add a `tenant_id` payload field to each point
   - Create a keyword payload index with `is_tenant=True`
   - Qdrant reorganizes storage to co-locate vectors of the same tenant
   - Filter by tenant in every query

2. **Custom sharding** (for fewer, larger tenants):
   - Use user-defined sharding (v1.7.0+)
   - Assign points to specific shards based on tenant
   - Route queries to specific shards

**Tiered Multitenancy** (v1.16+): Efficiently supports mixed workloads with both small and large tenants [7].

### 3.8 Quantization

Quantization reduces memory usage and increases search speed at the cost of some precision [14][15].

| Method | Compression | Speed Gain | Best For |
|--------|-------------|------------|----------|
| **Scalar (SQ)** | float32 -> int8 (4x) | Up to 2x | General use; best balance of compression and recall |
| **Product (PQ)** | Configurable compression ratio | Moderate | Very large datasets, memory-critical |
| **Binary (BQ)** | float32 -> 1-bit (32x) | Up to 40x | High-dimensional embeddings (>=1024 dims) |
| **1.5-bit** | Between binary and scalar | Variable | Improved BQ tradeoff |
| **2-bit** | Between 1.5-bit and scalar | Variable | Improved BQ tradeoff |

**Oversampling**: Retrieve more candidates than needed, then rescore top-k with original (unquantized) vectors for better accuracy.

**Asymmetric quantization**: Store vectors in binary form but query with scalar precision for smarter memory/accuracy tradeoff [7].

### 3.9 Replication and Sharding

- **Sharding**: Automatic (consistent hashing) or user-defined (v1.7.0+)
- **Replication**: Each shard managed by a ReplicaSet with configurable `replication_factor`
- **Fault tolerance**: If failed nodes < replication factor, cluster continues operating
- **Shard transfer**: Automatic rebalancing when nodes join/leave

### 3.10 Snapshots and Backups

- **Collection snapshots**: Contain all data, configuration, and indexes for a collection
- **Full snapshots**: Bundle all collection snapshots + alias mappings
- **Operations**: Create, download, restore (from URL, file URI, or uploaded file)
- **Integrity**: Optional SHA256 checksum verification
- **Note**: Collection aliases are NOT included in collection-level snapshots; they are in full snapshots

### 3.11 Collection Aliases

Aliases provide named references to collections, enabling:
- Zero-downtime collection swaps (blue-green deployments)
- A/B testing between collection versions
- Simplified client configuration

### 3.12 Recommend API

Suggest similar items based on positive and negative example points:
- Provide positive examples (items to find similar to)
- Provide negative examples (items to find dissimilar from)
- Supports `best_score` strategy for flexible recommendations
- Now part of the Universal Query API

### 3.13 Discovery API

Explore the vector space using context pairs:
- Define "positive-negative" context pairs to guide exploration
- Useful for finding items in a specific region of vector space
- Can be combined with target vectors
- Now part of the Universal Query API

### 3.14 Grouping

Group search results by a payload field:
- Returns top results per group (e.g., top result per document)
- Configurable `group_size` and `limit`
- Available via the `group_by` parameter in search/query endpoints

---

## 4. Distance Metrics

| Metric | Formula | Range | When to Use |
|--------|---------|-------|-------------|
| **Cosine** | 1 - cos(a, b) | [0, 2] | Most common for text embeddings; direction matters, not magnitude. Pre-normalized vectors get same result as Dot Product but faster |
| **Dot Product** | -dot(a, b) | (-inf, inf) | When both direction and magnitude matter; vectors must be normalized for similarity ranking. Many embedding models output normalized vectors |
| **Euclidean** | sqrt(sum((a-b)^2)) | [0, inf) | When absolute distances matter; spatial data, coordinates. Sensitive to magnitude |
| **Manhattan** | sum(abs(a-b)) | [0, inf) | Alternative to Euclidean; more robust to outliers in high dimensions |

**Recommendation**: Use Cosine for most text/embedding workloads. Use Dot Product if embeddings are already normalized (slightly faster than Cosine). Use Euclidean for spatial/coordinate data.

---

## 5. Deployment Options

### 5.1 Docker (Recommended for Development/Testing)

```bash
# Pull and run with persistent storage
docker pull qdrant/qdrant

docker run -p 6333:6333 -p 6334:6334 \
    -v "$(pwd)/qdrant_storage:/qdrant/storage:z" \
    qdrant/qdrant
```

Ports:
- **6333**: REST API + Web Dashboard (`localhost:6333/dashboard`)
- **6334**: gRPC API

### 5.2 Docker Compose

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

### 5.3 Local Binary

Download pre-built binaries from GitHub releases or build from source:
```bash
cargo build --release
./target/release/qdrant
```

### 5.4 Kubernetes (Helm Chart)

```bash
helm repo add qdrant https://qdrant.github.io/qdrant-helm
helm repo update
helm install qdrant qdrant/qdrant
```

The Helm chart supports:
- StatefulSets for data persistence
- Configurable replicas
- Resource limits and requests
- TLS and API key configuration
- PVC templates for storage

**Note**: For production Kubernetes deployments, Qdrant recommends their Private Cloud Enterprise Operator for zero-downtime upgrades, auto-scaling, monitoring, and disaster recovery [16].

### 5.5 Qdrant Cloud (Managed)

| Tier | Details |
|------|---------|
| **Free** | 1 GB RAM, 4 GB disk, no credit card required |
| **Standard** | Pay-as-you-go; ~$150-200/month for 8GB RAM, 2 vCPU |
| **Hybrid Cloud** | Starting at $0.014/hour; runs in your infrastructure |
| **Enterprise** | Custom contracts; $2,000-5,000+/month |

**Billing**: Hourly based on vCPU, RAM, storage, backup storage, and inference tokens.

**Regions**: Available on AWS, Google Cloud, and Azure across multiple global regions [17].

---

## 6. API and SDKs

### 6.1 REST API

Base URL: `http://localhost:6333`

Key endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/collections` | GET | List all collections |
| `/collections/{name}` | PUT | Create collection |
| `/collections/{name}` | DELETE | Delete collection |
| `/collections/{name}/points` | PUT | Upsert points |
| `/collections/{name}/points/search` | POST | Search nearest neighbors |
| `/collections/{name}/points/query` | POST | Universal Query API |
| `/collections/{name}/points/scroll` | POST | Paginate through points |
| `/collections/{name}/points/count` | POST | Count points |
| `/collections/{name}/points/delete` | POST | Delete points |
| `/collections/{name}/points/payload` | PUT | Set payload |
| `/collections/{name}/points/payload/delete` | POST | Delete payload keys |
| `/collections/{name}/snapshots` | POST | Create snapshot |
| `/collections/{name}/index` | PUT | Create payload index |

OpenAPI specification is available at the API reference [18].

### 6.2 gRPC API

Port: `6334` (external), `6335` (internal peer-to-peer)

- Defined using Protocol Buffers (proto3)
- Offers higher performance than REST for high-throughput workloads
- Same operations as REST API
- Recommended for production after initial development with REST

### 6.3 Official SDKs

| Language | Package | Install | Protocol |
|----------|---------|---------|----------|
| **Python** | `qdrant-client` | `pip install qdrant-client` | REST + gRPC |
| **JavaScript/TS** | `@qdrant/js-client-rest` | `npm install @qdrant/js-client-rest` | REST |
| **Rust** | `qdrant-client` | `cargo add qdrant-client` | gRPC |
| **Go** | `go-client` | `go get github.com/qdrant/go-client` | gRPC |
| **Java** | `qdrant-client` | Maven/Gradle | gRPC |
| **.NET** | `Qdrant.Client` | `dotnet add package Qdrant.Client` | gRPC |

### 6.4 Python SDK Examples

**Connection:**
```python
from qdrant_client import QdrantClient

# Remote server
client = QdrantClient(host="localhost", port=6333)

# With API key
client = QdrantClient(url="https://your-cluster.cloud.qdrant.io", api_key="your-key")

# In-memory (no server needed)
client = QdrantClient(":memory:")

# Local persistent storage (no server needed)
client = QdrantClient(path="path/to/db")

# Prefer gRPC for performance
client = QdrantClient(host="localhost", grpc_port=6334, prefer_grpc=True)

# Async client
from qdrant_client import AsyncQdrantClient
async_client = AsyncQdrantClient(host="localhost", port=6333)
```

**Create collection:**
```python
from qdrant_client.models import Distance, VectorParams

client.create_collection(
    collection_name="test_collection",
    vectors_config=VectorParams(size=4, distance=Distance.DOT),
)
```

**Upsert points:**
```python
from qdrant_client.models import PointStruct

operation_info = client.upsert(
    collection_name="test_collection",
    wait=True,
    points=[
        PointStruct(id=1, vector=[0.05, 0.61, 0.76, 0.74], payload={"city": "Berlin"}),
        PointStruct(id=2, vector=[0.19, 0.81, 0.75, 0.11], payload={"city": "London"}),
        PointStruct(id=3, vector=[0.36, 0.55, 0.47, 0.94], payload={"city": "Moscow"}),
    ],
)
```

**Search:**
```python
search_result = client.query_points(
    collection_name="test_collection",
    query=[0.2, 0.1, 0.9, 0.7],
    with_payload=False,
    limit=3
).points
```

**Filtered search:**
```python
from qdrant_client.models import Filter, FieldCondition, MatchValue

search_result = client.query_points(
    collection_name="test_collection",
    query=[0.2, 0.1, 0.9, 0.7],
    query_filter=Filter(
        must=[FieldCondition(key="city", match=MatchValue(value="London"))]
    ),
    with_payload=True,
    limit=3,
).points
```

### 6.5 JavaScript/TypeScript SDK Example

```typescript
import { QdrantClient } from '@qdrant/js-client-rest';

const client = new QdrantClient({ host: 'localhost', port: 6333 });

// Create collection
await client.createCollection('test_collection', {
    vectors: { size: 4, distance: 'Dot' },
});

// Upsert points
await client.upsert('test_collection', {
    wait: true,
    points: [
        { id: 1, vector: [0.05, 0.61, 0.76, 0.74], payload: { city: 'Berlin' } },
        { id: 2, vector: [0.19, 0.81, 0.75, 0.11], payload: { city: 'London' } },
    ],
});

// Search
const searchResult = await client.query('test_collection', {
    query: [0.2, 0.1, 0.9, 0.7],
    limit: 3,
});
```

---

## 7. Data Operations

### 7.1 Collection Management

```python
# Create collection with full config
client.create_collection(
    collection_name="my_collection",
    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
    hnsw_config=HnswConfigDiff(m=16, ef_construct=100),
    optimizers_config=OptimizersConfigDiff(
        default_segment_number=2,
        indexing_threshold=20000,
    ),
    replication_factor=2,
    write_consistency_factor=1,
    on_disk_payload=True,
)

# Delete collection
client.delete_collection("my_collection")

# List collections
collections = client.get_collections()

# Get collection info
info = client.get_collection("my_collection")
```

### 7.2 Upsert (Single and Batch)

```python
# Single or batch upsert
client.upsert(
    collection_name="my_collection",
    wait=True,  # Wait for operation to complete
    points=[
        PointStruct(id=1, vector=[...], payload={"key": "value"}),
        # ... more points for batch
    ],
)
```

For large-scale ingestion, use batch sizes of 100-1000 points per upsert call.

### 7.3 Search

```python
# Basic search
results = client.query_points(
    collection_name="my_collection",
    query=[0.1, 0.2, ...],
    limit=10,
    with_payload=True,
    with_vectors=False,
)

# Search with score threshold
results = client.query_points(
    collection_name="my_collection",
    query=[0.1, 0.2, ...],
    score_threshold=0.8,
    limit=10,
)
```

### 7.4 Scroll (Pagination)

```python
# First page
results, next_offset = client.scroll(
    collection_name="my_collection",
    limit=100,
    with_payload=True,
)

# Next page
results, next_offset = client.scroll(
    collection_name="my_collection",
    offset=next_offset,
    limit=100,
)
```

### 7.5 Count

```python
count = client.count(
    collection_name="my_collection",
    count_filter=Filter(
        must=[FieldCondition(key="city", match=MatchValue(value="Berlin"))]
    ),
    exact=True,  # Exact count (slower) vs approximate
)
```

### 7.6 Update/Delete Points

```python
# Delete by IDs
client.delete(
    collection_name="my_collection",
    points_selector=[1, 2, 3],
)

# Delete by filter
client.delete(
    collection_name="my_collection",
    points_selector=FilterSelector(
        filter=Filter(must=[FieldCondition(key="city", match=MatchValue(value="Berlin"))])
    ),
)
```

### 7.7 Set/Delete Payload

```python
# Set payload
client.set_payload(
    collection_name="my_collection",
    payload={"new_field": "new_value"},
    points=[1, 2, 3],
)

# Delete payload keys
client.delete_payload(
    collection_name="my_collection",
    keys=["old_field"],
    points=[1, 2, 3],
)
```

---

## 8. Advanced Features

### 8.1 Distributed Mode

**Raft consensus**: Used for cluster topology and collection structure operations. Ensures all operations are durable and eventually executed by all nodes [5].

**Important**: Point operations (upsert, search, delete) do NOT go through Raft consensus -- they use direct peer-to-peer communication for low overhead. Only collection-level operations (create, delete, update config) go through consensus [5].

**Enabling distributed mode:**
```bash
./qdrant --cluster --bootstrap http://first-node:6335
```

### 8.2 Replication Configuration

```python
client.create_collection(
    collection_name="replicated_collection",
    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
    replication_factor=3,         # 3 copies of each shard
    write_consistency_factor=2,   # 2 replicas must ACK writes
)
```

### 8.3 Write Consistency Levels

Three mechanisms [5]:

1. **Write Ordering**: Controls which peer processes the update
   - `weak` -- Any peer (fastest)
   - `medium` -- Leader preferred
   - `strong` -- Leader required with consensus

2. **Consistency Factor**: Number of replicas that must acknowledge
   - Formula for strong consistency: `write_consistency_factor = (replication_factor / 2) + 1`

3. **Clock Tags**: Version operations to prevent stale updates and detect conflicts

### 8.4 Sharding Strategies

- **Automatic** (default): Consistent hashing distributes points across shards
- **Custom/User-defined** (v1.7.0+): Upload points to specific shards; route queries to specific shards

### 8.5 Snapshot Operations

```python
# Create snapshot
snapshot_info = client.create_snapshot("my_collection")

# List snapshots
snapshots = client.list_snapshots("my_collection")

# Full snapshot (all collections + aliases)
full_snapshot = client.create_full_snapshot()
```

**Restore via CLI:**
```bash
./qdrant --snapshot /path/to/snapshot.snapshot
```

**Restore via API:**
```
POST /collections/{name}/snapshots/recover
{"location": "https://example.com/snapshot.snapshot"}
```

### 8.6 On-Disk vs In-Memory Configuration

```python
# On-disk vectors
client.create_collection(
    collection_name="ondisk_collection",
    vectors_config=VectorParams(
        size=768,
        distance=Distance.COSINE,
        on_disk=True,              # Store vectors on disk
    ),
    hnsw_config=HnswConfigDiff(
        on_disk=True,              # Store HNSW index on disk
    ),
    on_disk_payload=True,          # Store payloads on disk
)
```

### 8.7 Memmap Threshold

```python
optimizers_config=OptimizersConfigDiff(
    memmap_threshold=50000,  # Switch to mmap when segment exceeds 50K vectors
)
```

### 8.8 WAL Configuration

Configurable via YAML or environment variables:
```yaml
storage:
  wal:
    wal_capacity_mb: 32
    wal_segments_ahead: 0
```

---

## 9. Performance Tuning

### 9.1 HNSW Parameter Tuning

| Goal | `m` | `ef_construct` | `ef` (search) |
|------|-----|----------------|---------------|
| Fast ingestion, low RAM | 8 | 64 | 64 |
| Balanced (default) | 16 | 100 | 128 |
| High recall | 32 | 200 | 256 |
| Maximum recall | 64 | 512 | 512 |

### 9.2 Quantization Impact

| Method | Memory Reduction | Speed Impact | Recall Impact |
|--------|-----------------|--------------|---------------|
| Scalar (int8) | ~75% (4x) | ~2x faster | Minimal (<1%) |
| Binary (1-bit) | ~97% (32x) | ~40x faster | Noticeable; best for dims >= 1024 |
| Product | Configurable | Moderate gain | Moderate loss |

**Oversampling** and **rescoring with original vectors** can recover lost precision from quantization.

### 9.3 Batch Size Recommendations

- **Upsert batch size**: 100-1000 points per call for optimal throughput
- **Parallel upserts**: Use multiple threads/connections for large ingestion jobs
- **Wait parameter**: Set `wait=False` for fire-and-forget (faster) or `wait=True` for confirmed writes

### 9.4 Payload Index Impact

- Always create payload indexes for fields used in filters
- Without indexes, Qdrant loads entire payloads from disk to check conditions
- Keyword indexes for exact match, integer/float for range queries

### 9.5 Segment Optimization

- Fewer, larger segments = faster search (fewer segments to scan)
- More segments = faster indexing
- Use `default_segment_number` to control target segment count
- The optimizer automatically merges small segments

### 9.6 RAM Sizing Rules of Thumb

**Full in-memory (no quantization):**
```
RAM = num_vectors * vector_dimension * 4 bytes * 1.5
```
(1.5x multiplier accounts for indexes, metadata, temp segments)

Example: 1M vectors x 1024 dims = ~5.72 GB RAM

**With mmap storage:**
~135 MB RAM per 1 million vectors (metadata + index hot portions only) [19].

**With scalar quantization:**
~25% of full in-memory requirement.

### 9.7 Benchmarks

- Qdrant consistently ranks in the top tier alongside Pinecone and Milvus/Zilliz for low-latency queries [20]
- 10-100ms query times typical on 1M-10M vector datasets
- Scalar quantization achieves up to 4x lower memory and 2x performance increase with minimal recall loss [14]
- Binary quantization achieves up to 40x speed improvement for high-dimensional embeddings [15]
- GPU-accelerated HNSW indexing provides up to 10x faster ingestion [7]

---

## 10. Security

### 10.1 API Key Authentication

```yaml
# config.yaml or environment variable
service:
  api_key: "your-secret-api-key"
  read_only_api_key: "your-read-only-key"  # Optional
```

Or via environment variable:
```bash
QDRANT__SERVICE__API_KEY=your-secret-api-key
```

Two key types:
- **Regular API key**: Full read/write/delete access
- **Read-only API key**: Read and search operations only

**Granular database API keys** available since v1.11.0 [21].

### 10.2 JWT-Based RBAC (v1.9.0+)

For granular access control [21]:
- Enable JWT authentication alongside API key
- Create tokens with specific permissions per collection
- Control read/write access at the collection level
- Token-based access enables multi-tenant security

```yaml
service:
  api_key: "your-api-key"
  jwt_rbac: true
```

### 10.3 TLS Configuration

```yaml
service:
  enable_tls: true
tls:
  cert: /path/to/cert.pem
  key: /path/to/key.pem
  ca_cert: /path/to/ca.pem  # Optional, for mutual TLS
```

**Important**: Always enable TLS when using API key authentication to prevent credential sniffing and MitM attacks [21].

### 10.4 Network Security

- Restrict port access (6333, 6334) via firewall rules
- Use private networks for inter-node communication (port 6335)
- Consider reverse proxy (nginx) for additional security layers

---

## 11. Production Guidance

### 11.1 Resource Sizing

| Scale | RAM (in-memory) | RAM (mmap) | RAM (quantized) |
|-------|-----------------|------------|-----------------|
| 1M vectors (768 dims) | ~4.3 GB | ~135 MB | ~1.1 GB |
| 10M vectors (768 dims) | ~43 GB | ~1.35 GB | ~11 GB |
| 100M vectors (768 dims) | ~430 GB | ~13.5 GB | ~110 GB |
| 1B vectors (128 dims) | ~150 GB | Variable | ~38 GB |

### 11.2 High Availability Setup

1. Deploy 3+ nodes minimum
2. Set `replication_factor >= 2` (ideally 3)
3. Set `write_consistency_factor = (replication_factor / 2) + 1`
4. Use Kubernetes StatefulSets with anti-affinity rules
5. Place replicas in different availability zones

### 11.3 Backup Strategies

1. **Scheduled snapshots**: Create collection snapshots on a regular schedule
2. **Full snapshots**: Include all collections and alias mappings
3. **External storage**: Download snapshots to S3/GCS/Azure Blob
4. **Qdrant Cloud**: Automated backups with configurable retention

### 11.4 Monitoring

**Prometheus metrics endpoint**: `GET /metrics` on each node [22]

Key metrics to monitor:
- Search latency (p50, p95, p99)
- Upsert throughput
- Collection size and point counts
- Memory usage
- Segment counts and optimizer activity
- gRPC/REST request rates and errors

**Setup**: Configure Prometheus to scrape `/metrics` from each node individually; use Grafana for dashboards.

### 11.5 Upgrade Procedures

- Use rolling upgrades in distributed deployments
- Create full snapshots before upgrading
- Qdrant maintains backward compatibility for stored data
- Test with staging cluster before production upgrade
- Helm chart supports `helm upgrade` for Kubernetes deployments

---

## 12. Integrations

### 12.1 LLM/AI Frameworks

| Framework | Integration Type | Notes |
|-----------|-----------------|-------|
| **LangChain** | `QdrantVectorStore` | Supports dense, sparse, and hybrid retrieval modes via Query API [23] |
| **LlamaIndex** | Vector Store index | Data ingestion, indexing, and retrieval [24] |
| **Haystack** | Document Store | Integration as document store component [25] |
| **Semantic Kernel** | Persistent memory | Memory backend for Microsoft Semantic Kernel [26] |
| **OpenAI** | ChatGPT retrieval plugin | Memory backend for ChatGPT [27] |
| **Cohere** | Embedding + reranking | Embedding generation and reranking support |
| **AutoGen** | Vector memory | Agent memory backend |
| **CrewAI** | Knowledge base | RAG knowledge source |

### 12.2 Embedding Tools

- **FastEmbed** (by Qdrant): Lightweight, fast embedding model library
- **Qdrant MCP Server**: Model Context Protocol integration for AI agents
- **Qdrant Edge**: Lightweight vector database for edge/IoT devices [7]

### 12.3 Data Platforms

- **Airbyte**: Data ingestion connector
- **Unstructured**: Document processing pipeline
- **Apache Spark**: Batch processing integration

---

## 13. Use Cases

| Use Case | Description | Key Features Used |
|----------|-------------|-------------------|
| **RAG** | Retrieve relevant context for LLM generation | Semantic search, filtering, hybrid search |
| **Semantic Search** | Find content by meaning, not keywords | Dense vectors, payload filtering |
| **Recommendation** | Suggest similar items based on preferences | Recommend API, positive/negative examples |
| **Anomaly Detection** | Identify outliers in high-dimensional data | Distance-based outlier detection |
| **Image Search** | Find visually similar images | Image embeddings, multimodal vectors |
| **Deduplication** | Detect duplicate or near-duplicate records | Similarity threshold search |
| **Code Search** | Find similar code snippets | Code embeddings, filtering by language |
| **Collaborative Filtering** | User/item interaction-based recommendations | Sparse vectors, named vectors |
| **Document Clustering** | Group similar documents together | Grouping API, payload aggregation |

---

## 14. Comparison with Alternatives

| Feature | Qdrant | Pinecone | Weaviate | Milvus | Chroma | pgvector |
|---------|--------|----------|----------|--------|--------|----------|
| **License** | Apache 2.0 | Proprietary | BSD-3 | Apache 2.0 | Apache 2.0 | PostgreSQL |
| **Language** | Rust | Proprietary | Go | Go/C++ | Python | C |
| **Self-hosted** | Yes | No (cloud only) | Yes | Yes | Yes | Yes (Postgres extension) |
| **Cloud managed** | Yes | Yes (primary) | Yes | Yes (Zilliz) | No | Various |
| **Hybrid search** | Yes (native) | Yes | Yes | Yes | Basic | No (native) |
| **Filtering** | Advanced (filterable HNSW) | Yes | Yes | Yes | Basic | SQL WHERE |
| **RBAC** | JWT-based | Yes | Yes | Yes | No | Postgres roles |
| **Max scale** | Billions | Billions | Billions | Billions | Millions | 10-100M practical |
| **Quantization** | Scalar, Product, Binary, 1.5-bit, 2-bit | Yes | PQ, BQ | Various | No | No (halfvec only) |
| **Named vectors** | Yes | Namespaces | Yes | Multiple | No | No |
| **Sparse vectors** | Yes | Yes | No (native) | Yes | No | No |
| **Ease of setup** | Easy | Easiest | Moderate | Complex | Easiest | Easy (if using Postgres) |

### Key Differentiators for Qdrant [20][28]:

1. **Rust performance**: Memory safety without garbage collection pauses
2. **Filterable HNSW**: Maintains search quality even with high filter selectivity
3. **Rich quantization options**: Most quantization strategies of any open-source vector DB
4. **Strong multitenancy**: Dedicated tenant-aware storage optimization
5. **Universal Query API**: Single endpoint for all search modes
6. **Payload filtering depth**: Most expressive filtering among vector DBs (nested, geo, full-text, datetime)
7. **Local mode**: Python client can run without a server (`":memory:"` or file-based)

---

## 15. Limitations and Tradeoffs

### What Qdrant Does NOT Do Well

1. **Many small collections anti-pattern**: Creating a collection per user/document leads to significant resource overhead. Use multitenancy with payload filtering instead [9].

2. **Heavily filtered search on small result sets**: When filters are extremely selective, performance may degrade compared to specialized full-text search engines like Meilisearch or Elasticsearch [29].

3. **Large limit/offset pagination**: Performance degrades with very large `limit` or `offset` values. Use scroll API for deep pagination instead.

4. **Missing payload indexes**: Without proper payload indexes, filtered searches require loading entire payloads from disk -- a common source of poor performance [9].

5. **No full SQL/relational operations**: Qdrant is a vector database, not a relational database. Complex joins, transactions, and aggregations are not supported.

6. **No built-in embedding generation** (core product): You must generate embeddings externally (though FastEmbed and Cloud Inference are available as add-ons).

7. **Quantization precision tradeoff**: Binary quantization works poorly for embeddings under 1024 dimensions [15].

8. **Vertical scaling limits**: Single-node deployments have natural hardware limits; horizontal scaling requires distributed mode.

9. **Distributed mode complexity**: While functional, the open-source Helm chart lacks the zero-downtime upgrade, auto-scaling, and disaster recovery features of the commercial Private Cloud operator [16].

10. **No native ACID transactions**: Point operations are eventually consistent in distributed mode; no cross-collection transactions.

### When NOT to Use Qdrant

- **Purely relational data**: Use PostgreSQL, MySQL, etc.
- **Small datasets (<10K vectors)**: pgvector or even brute-force search suffices
- **Heavy text search without vectors**: Use Elasticsearch, Meilisearch, or Typesense
- **Need for ACID transactions across operations**: Use a traditional RDBMS
- **Budget-zero, minimal-ops prototype**: Chroma may be simpler for quick prototyping

---

## 16. Current Version Notes (v1.17.x, March 2026)

### Recent Major Additions (2025-2026) [7]

| Version | Key Features |
|---------|-------------|
| **v1.13** | GPU-accelerated HNSW indexing, strict mode, custom storage engine |
| **v1.14** | Incremental HNSW indexing, HNSW graph compression |
| **v1.15** | 1.5-bit and 2-bit quantization, asymmetric quantization, improved text filtering |
| **v1.16** | Tiered multitenancy, disk-efficient vector search, inline storage |
| **v1.17** | Latest stable (March 2026) |

### 2026 Roadmap Highlights [7]

- 4-bit quantization
- Read-write segregation
- Block storage integration
- Relevance feedback
- Expanded inference capabilities
- Fully scalable multitenancy
- Faster horizontal scaling
- Read-only replicas

---

## Methodology

### Search Queries Used
- "Qdrant vector database latest stable version 2025 2026 features release"
- "Qdrant architecture HNSW segments collections points storage model"
- "Qdrant Python SDK qdrant-client examples"
- "Qdrant deployment Docker Kubernetes Helm chart"
- "Qdrant Cloud pricing managed service regions"
- "Qdrant distributed mode replication sharding Raft consensus"
- "Qdrant performance tuning HNSW quantization benchmarks"
- "Qdrant security API key TLS RBAC"
- "Qdrant production sizing RAM monitoring Prometheus"
- "Qdrant vs Pinecone vs Weaviate vs Milvus vs Chroma vs pgvector"
- "Qdrant integrations LangChain LlamaIndex Haystack"
- "Qdrant limitations tradeoffs"
- "Qdrant hybrid search fusion recommend discovery API"
- "Qdrant use cases RAG semantic search"
- "Qdrant multitenancy named vectors payload indexing"
- "Qdrant snapshots backup restore aliases"

### Sources Consulted
Official documentation, GitHub repository, release notes, blog posts, third-party comparisons, community discussions, and API references.

### Evaluation Criteria
Prioritized official Qdrant documentation and GitHub over third-party sources. Cross-referenced key claims across multiple sources. Marked confidence levels where applicable.

---

## Confidence Assessment

### High Confidence
- Core architecture (collections, points, segments, HNSW)
- Current version (v1.17.1) and license (Apache 2.0)
- API endpoints and SDK availability
- Distance metrics and their use cases
- Docker deployment commands and port assignments
- Quantization types and their general impact
- Security features (API key, TLS, JWT RBAC)
- Integration ecosystem (LangChain, LlamaIndex, etc.)

### Medium Confidence
- Exact RAM sizing formulas (vary by workload and configuration)
- Benchmark numbers (hardware-dependent)
- Qdrant Cloud pricing (may change; verified as of early 2026)
- Detailed 2026 roadmap items (subject to change)

### Low Confidence
- Specific performance numbers for GPU-accelerated indexing (limited public benchmarks)
- Enterprise pricing details (custom contracts vary significantly)

### Knowledge Gaps
- Detailed internal storage engine implementation specifics
- Comprehensive production incident patterns and recovery playbooks
- Detailed cost optimization strategies for very large deployments

---

## Sources and References

[1] [Qdrant Documentation Overview](https://qdrant.tech/documentation/) - [Official]
[2] [Qdrant GitHub Repository](https://github.com/qdrant/qdrant) - [Official]
[3] [Qdrant Architecture - DeepWiki](https://deepwiki.com/qdrant/qdrant/1-introduction-to-qdrant) - [Community]
[4] [HNSW Indexing Fundamentals - Qdrant Course](https://qdrant.tech/course/essentials/day-2/what-is-hnsw/) - [Official]
[5] [Distributed Deployment - Qdrant](https://qdrant.tech/documentation/operations/distributed_deployment/) - [Official]
[6] [The Theory Behind HNSW in Qdrant - Medium](https://medium.com/@wriath18/the-theory-behind-hnsw-algorithm-in-qdrant-vector-database-f274df648e0e) - [Community]
[7] [Qdrant 2025 Recap](https://qdrant.tech/blog/2025-recap/) - [Official]
[8] [Filtering - Qdrant Documentation](https://qdrant.tech/documentation/search/filtering/) - [Official]
[9] [Database Optimization FAQ - Qdrant](https://qdrant.tech/documentation/faq/database-optimization/) - [Official]
[10] [Multitenancy in Qdrant](https://qdrant.tech/articles/multitenancy/) - [Official]
[11] [Sparse Vectors in Qdrant](https://qdrant.tech/articles/sparse-vectors/) - [Official]
[12] [Hybrid Search with Query API](https://qdrant.tech/articles/hybrid-search/) - [Official]
[13] [Multiple Vectors Per Object](https://qdrant.tech/articles/storing-multiple-vectors-per-object-in-qdrant/) - [Official]
[14] [Scalar Quantization Article](https://qdrant.tech/articles/scalar-quantization/) - [Official]
[15] [Binary Quantization - 40x Faster](https://qdrant.tech/articles/binary-quantization/) - [Official]
[16] [Installation - Qdrant](https://qdrant.tech/documentation/operations/installation/) - [Official]
[17] [Qdrant Cloud Pricing](https://qdrant.tech/pricing/) - [Official]
[18] [Qdrant API Reference](https://api.qdrant.tech/api-reference) - [Official]
[19] [Minimal RAM for Million Vectors](https://qdrant.tech/articles/memory-consumption/) - [Official]
[20] [Best Vector Databases 2026 Comparison](https://www.firecrawl.dev/blog/best-vector-databases) - [Industry]
[21] [Security - Qdrant](https://qdrant.tech/documentation/operations/security/) - [Official]
[22] [Prometheus Monitoring with Qdrant](https://qdrant.tech/documentation/tutorials-and-examples/hybrid-cloud-prometheus/) - [Official]
[23] [Qdrant LangChain Integration](https://docs.langchain.com/oss/python/integrations/vectorstores/qdrant) - [Official]
[24] [LlamaIndex - Qdrant](https://qdrant.tech/documentation/frameworks/llama-index/) - [Official]
[25] [Haystack Integration](https://qdrant.tech/documentation/frameworks/) - [Official]
[26] [Semantic Kernel Integration](https://qdrant.tech/documentation/frameworks/) - [Official]
[27] [OpenAI ChatGPT Retrieval Plugin](https://cookbook.openai.com/examples/vector_databases/qdrant/using_qdrant_for_embeddings_search) - [Official]
[28] [Vector Database Comparison 2025](https://liquidmetal.ai/casesAndBlogs/vector-comparison/) - [Industry]
[29] [Meilisearch vs Qdrant Tradeoffs](https://blog.kerollmops.com/meilisearch-vs-qdrant-tradeoffs-strengths-and-weaknesses) - [Community]
[30] [Qdrant GitHub Releases](https://github.com/qdrant/qdrant/releases) - [Official]
[31] [Python Client Documentation](https://python-client.qdrant.tech/) - [Official]
[32] [Qdrant Quickstart Guide](https://qdrant.tech/documentation/quickstart/) - [Official]
[33] [Qdrant Helm Chart](https://github.com/qdrant/qdrant-helm) - [Official]
[34] [Capacity Planning Guide](https://qdrant.tech/documentation/guides/capacity-planning/) - [Official]
[35] [Vector Search Resource Optimization](https://qdrant.tech/articles/vector-search-resource-optimization/) - [Official]

---

*Report generated by Research Agent*
*File location: c:/MY-WorkSpace/rag/researches/2026-03-31_qdrant-vector-database-comprehensive-guide.md*

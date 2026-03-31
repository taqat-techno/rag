# Sentence Transformers User Guide

> A comprehensive, practical guide for developers, ML engineers, data scientists, and AI application builders.
> Covers Sentence Transformers v5.3.x (March 2026). Written for a technical audience.

---

## Table of Contents

- [1. Introduction](#1-introduction)
- [2. Fundamentals](#2-fundamentals)
- [3. Core Concepts and Architecture](#3-core-concepts-and-architecture)
- [4. Key Features](#4-key-features)
- [5. Installation and Setup](#5-installation-and-setup)
- [6. Quick Start](#6-quick-start)
- [7. Model Selection Guide](#7-model-selection-guide)
- [8. Embeddings and Similarity](#8-embeddings-and-similarity)
- [9. API and SDK Usage](#9-api-and-sdk-usage)
- [10. Training and Fine-Tuning](#10-training-and-fine-tuning)
- [11. Evaluation](#11-evaluation)
- [12. Advanced Usage](#12-advanced-usage)
- [13. Sentence Transformers for Search, Retrieval, and RAG](#13-sentence-transformers-for-search-retrieval-and-rag)
- [14. Deployment and Production Guidance](#14-deployment-and-production-guidance)
- [15. Performance Optimization](#15-performance-optimization)
- [16. Common Pitfalls](#16-common-pitfalls)
- [17. Comparison and Tradeoffs](#17-comparison-and-tradeoffs)
- [18. Best Practices Checklist](#18-best-practices-checklist)
- [19. Quick Start Recap](#19-quick-start-recap)
- [20. References](#20-references)
- [Appendix A: Feature Summary](#appendix-a-feature-summary)
- [Appendix B: Recommended Learning Path](#appendix-b-recommended-learning-path)
- [Appendix C: Top 10 Implementation Tips](#appendix-c-top-10-implementation-tips)

---

## 1. Introduction

### What Sentence Transformers Is

Sentence Transformers (also known as SBERT) is a Python framework for computing sentence, paragraph, and image embeddings using transformer models. Built on top of Hugging Face Transformers and PyTorch, it provides a simple API to turn variable-length text into fixed-size dense vectors that capture semantic meaning.

| Attribute | Value |
|-----------|-------|
| **Current Version** | 5.3.0 (March 12, 2026) |
| **License** | Apache 2.0 |
| **GitHub Stars** | ~18,500 |
| **Python Requirement** | 3.10+ |
| **PyTorch Requirement** | 1.11.0+ |
| **Pretrained Models** | 15,000+ on Hugging Face Hub |
| **Original Creator** | Nils Reimers (UKP Lab, TU Darmstadt) |
| **Current Maintainer** | Tom Aarsen (Hugging Face) |

### Who It Is For

- **ML engineers** building semantic search, recommendation, or classification systems
- **Backend developers** adding embedding-based features to applications
- **Data scientists** analyzing text similarity, clustering, or deduplication
- **AI engineers** building RAG pipelines, chatbots, or information retrieval systems

### Problems It Solves

1. **Semantic text representation** — Convert text into vectors that capture meaning, not just keywords
2. **Efficient similarity computation** — Pre-compute embeddings once, compare millions of pairs in milliseconds
3. **Training and fine-tuning** — Adapt embedding models to specific domains with 30+ loss functions
4. **Cross-lingual similarity** — Compare text meaning across 100+ languages
5. **Retrieval and reranking** — Two-stage retrieval with bi-encoders (fast) and cross-encoders (accurate)

### High-Level Feature Summary

| Category | Highlights |
|----------|------------|
| **Model Types** | SentenceTransformer (dense), CrossEncoder (reranker), SparseEncoder (SPLADE) |
| **Embeddings** | Dense, sparse, Matryoshka (variable-dim), quantized (int8, binary) |
| **Tasks** | Similarity, search, clustering, classification, paraphrase mining, reranking |
| **Training** | 30+ loss functions, SentenceTransformerTrainer (HF Trainer-based) |
| **Optimization** | ONNX, OpenVINO, fp16/bf16, multi-GPU, multi-process encoding |
| **Backends** | PyTorch, ONNX Runtime, OpenVINO |
| **Integrations** | LangChain, LlamaIndex, Qdrant, Pinecone, Chroma, Weaviate, Milvus |

---

## 2. Fundamentals

### Transformer Embeddings Overview

Transformer models (BERT, RoBERTa, etc.) process text as sequences of tokens and produce contextualized token-level embeddings. Each token's representation is informed by all other tokens in the input via self-attention. However, these token-level outputs are not directly usable as a single "text embedding" — they must be aggregated (pooled) into a fixed-size vector.

### Sentence Embeddings Explained

A sentence embedding is a single dense vector (typically 384-4096 dimensions) that represents the meaning of an entire sentence, paragraph, or short document. Key properties:

- **Fixed size**: Regardless of input length, the output is always the same dimensionality
- **Semantic**: Similar meanings produce similar vectors; dissimilar meanings produce distant vectors
- **Composable**: Embeddings can be compared, clustered, classified, and stored in vector databases

### Semantic Similarity Basics

Two texts are semantically similar if they convey related or equivalent meaning. Semantic similarity differs from lexical similarity:

| Pair | Lexical Similarity | Semantic Similarity |
|------|-------------------|---------------------|
| "dog" / "canine" | Low (different words) | High (same meaning) |
| "bank" (river) / "bank" (finance) | High (same word) | Low (different meanings) |
| "How old are you?" / "What is your age?" | Low (different words) | High (same meaning) |

Sentence Transformers encodes texts into vectors where cosine similarity between vectors correlates with semantic similarity.

### Why Sentence Transformers Exists

Using raw BERT for sentence similarity is extremely slow. Comparing 10,000 sentences requires 49,995,000 forward passes through the model (each pair must be processed jointly). SBERT solves this by encoding each sentence independently into a fixed-size vector — 10,000 sentences require only 10,000 forward passes, and all pairwise similarities are computed via fast matrix multiplication.

| Approach | 10,000 sentences pairwise | Time (estimate) |
|----------|--------------------------|-----------------|
| BERT cross-encoder | ~50M forward passes | ~65 hours |
| SBERT bi-encoder | 10,000 encodes + matrix multiply | ~5 seconds |

### Bi-Encoder vs. Cross-Encoder Overview

| Aspect | Bi-Encoder (SentenceTransformer) | Cross-Encoder |
|--------|----------------------------------|---------------|
| **How it works** | Encodes each text independently → compares vectors | Encodes both texts jointly through one transformer |
| **Speed** | Fast (encode once, compare many) | Slow (must process every pair) |
| **Quality** | Good | Superior (full cross-attention) |
| **Scalability** | Excellent (precompute embeddings) | Poor for large candidate sets |
| **Use case** | Initial retrieval from large corpus | Reranking top-k results |
| **Output** | Dense/sparse vectors | Similarity score (scalar) |

Common production pattern: bi-encoder retrieves top-100 candidates → cross-encoder reranks to top-10.

---

## 3. Core Concepts and Architecture

### SentenceTransformer Abstraction

The `SentenceTransformer` class is a modular pipeline of sequential PyTorch modules. When a model is loaded, a `modules.json` file defines which modules compose the pipeline and in what order.

```
Input Text → Tokenizer → Transformer Backbone → Pooling → [Dense] → [Normalize] → Embedding
```

Each component is independently configurable and stored in its own directory within the model.

### Tokenization

The tokenizer converts raw text into token IDs that the transformer understands. Different backbones use different tokenizers:

| Backbone | Tokenizer | Vocab Size |
|----------|-----------|------------|
| BERT | WordPiece | ~30,000 |
| RoBERTa | BPE | ~50,000 |
| MPNet | BPE | ~30,000 |
| XLM-RoBERTa | SentencePiece | ~250,000 |

Texts exceeding `max_seq_length` are automatically truncated. Most models have a limit of 256-512 tokens.

### Transformer Backbone

The pre-trained transformer model (BERT, RoBERTa, MPNet, etc.) that produces contextualized token-level embeddings. The backbone is the largest component and determines:
- Embedding quality (number of layers, attention heads)
- Supported languages (training data coverage)
- Maximum sequence length
- Computational cost

### Pooling Layers

The pooling module aggregates variable-length token embeddings into a single fixed-size vector:

| Strategy | Description | When to Use |
|----------|-------------|-------------|
| **Mean Pooling** | Average all token embeddings (excluding padding) | Default and recommended; best empirical results |
| **CLS Token** | Use the [CLS] token embedding | When the model was specifically trained this way |
| **Max Pooling** | Element-wise max across all tokens | Captures strongest signal per dimension |
| **Weighted Mean** | Position-weighted average (later tokens weighted more) | Rarely used |

> **Tip:** Don't change the pooling strategy of a pretrained model — it was trained with a specific strategy. Only choose pooling when training from scratch.

### Embedding Generation Pipeline

When you call `model.encode(texts)`:

1. **Prompt prepending**: If configured, prepend query/document prefixes (e.g., "query: " for E5 models)
2. **Length sorting**: Optionally sort by length to minimize padding waste within batches
3. **Batching**: Split into batches of `batch_size` (default 32)
4. **Tokenization**: Convert text to token IDs, truncate at `max_seq_length`
5. **Forward pass**: Tokens → Transformer → Pooling → Optional Dense → Optional Normalize
6. **Precision conversion**: Optionally quantize to int8, binary, etc.
7. **Output**: Return as numpy arrays (default), PyTorch tensors, or list

### Similarity Functions

| Function | Range | Best For | Notes |
|----------|-------|----------|-------|
| **Cosine Similarity** | [-1, 1] | Most use cases | Direction-based; ignores magnitude |
| **Dot Product** | (-∞, ∞) | Normalized embeddings | Faster than cosine when embeddings are unit-length |
| **Euclidean Distance** | [0, ∞) | Distance-based tasks | Sensitive to magnitude |
| **Manhattan Distance** | [0, ∞) | Outlier-robust tasks | L1 norm |

The `model.similarity()` method uses whichever function was set at initialization via `similarity_fn_name`.

### Dense Embeddings and Retrieval Concepts

Dense retrieval uses dense vector representations for both queries and documents. The core idea:

1. **Offline**: Encode all documents into embeddings, store in a vector database
2. **Online**: Encode the query, find nearest neighbors in the vector database
3. **Result**: Return documents whose embeddings are closest to the query embedding

This is fundamentally different from sparse retrieval (BM25/TF-IDF), which matches on exact term overlap.

---

## 4. Key Features

### Pretrained Models

Over 15,000 models available on Hugging Face Hub, ranging from lightweight (22M parameters, 384 dimensions) to large (7B+ parameters, 4096 dimensions). Models are downloaded automatically on first use and cached locally.

### Semantic Similarity

Compute how semantically similar two texts are:

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("all-MiniLM-L6-v2")
emb = model.encode(["The cat sat on the mat", "A feline rested on the rug"])
similarity = model.similarity(emb, emb)
# similarity[0][1] ≈ 0.75 (high — same meaning)
```

### Semantic Search

Find the most relevant documents for a query:

```python
from sentence_transformers.util import semantic_search

query_emb = model.encode("How does photosynthesis work?")
corpus_emb = model.encode(corpus_texts)
results = semantic_search(query_emb, corpus_emb, top_k=5)
```

### Clustering

Group semantically similar texts:

```python
from sentence_transformers.util import community_detection

embeddings = model.encode(sentences)
clusters = community_detection(embeddings, threshold=0.75, min_community_size=5)
```

Also supports k-means and agglomerative clustering via scikit-learn on the embeddings.

### Classification Support

Embeddings can be used as features for downstream classifiers (logistic regression, SVM, neural networks). Fine-tuned models with `SoftmaxLoss` can also perform direct text classification.

### Paraphrase Detection

Identify semantically equivalent sentence pairs within a corpus:

```python
from sentence_transformers.util import paraphrase_mining

paraphrases = paraphrase_mining(model, sentences, top_k=100)
for score, i, j in paraphrases:
    print(f"{score:.2f}: '{sentences[i]}' ↔ '{sentences[j]}'")
```

### Reranking

Cross-encoders rerank candidate documents by processing query-document pairs jointly:

```python
from sentence_transformers import CrossEncoder

reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L6-v2")
ranked = reranker.rank("What is machine learning?", candidate_passages)
```

### Fine-Tuning Support

Full training pipeline with `SentenceTransformerTrainer`, 30+ loss functions, evaluators, multi-dataset training, hard negative mining, and Matryoshka dimension training.

### Evaluation Utilities

Built-in evaluators for STS, information retrieval (MRR, MAP, NDCG), triplet ranking, binary classification, paraphrase mining, and multi-task benchmarking (NanoBEIR).

### Integration Ecosystem

Direct compatibility with LangChain, LlamaIndex, Haystack, and all major vector databases (Qdrant, Pinecone, Chroma, Weaviate, Milvus, pgvector, FAISS, Elasticsearch).

---

## 5. Installation and Setup

### Installation with pip

```bash
# Basic installation
pip install -U sentence-transformers

# With ONNX GPU support
pip install sentence-transformers[onnx-gpu]

# With ONNX CPU support
pip install sentence-transformers[onnx]

# With OpenVINO support
pip install sentence-transformers[openvino]
```

### Conda Installation

```bash
conda install -c conda-forge sentence-transformers
```

### Core Dependencies

| Package | Purpose |
|---------|---------|
| `torch` (>= 1.11.0) | Deep learning framework |
| `transformers` (>= 4.34.0) | Hugging Face model loading |
| `huggingface-hub` | Model download/upload |
| `tokenizers` | Fast tokenization |
| `scipy` | Scientific computing |
| `scikit-learn` | Clustering, evaluation |
| `tqdm` | Progress bars |
| `Pillow` (optional) | Image support for CLIP models |

### GPU Setup

For NVIDIA CUDA acceleration:

```bash
# Install PyTorch with CUDA 12.1
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

Apple Silicon users get MPS acceleration automatically. The library auto-detects available devices.

### Verification

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("all-MiniLM-L6-v2")
embeddings = model.encode(["Hello world", "Testing installation"])
print(f"Shape: {embeddings.shape}")      # (2, 384)
print(f"Device: {model.device}")          # cuda:0 or cpu or mps
print(f"Max seq length: {model.max_seq_length}")  # 256
```

---

## 6. Quick Start

### Load a Pretrained Model

```python
from sentence_transformers import SentenceTransformer

# Auto-downloads from Hugging Face Hub on first use
model = SentenceTransformer("all-MiniLM-L6-v2")

# Specify device explicitly
model = SentenceTransformer("all-MiniLM-L6-v2", device="cuda")
```

### Encode Text

```python
sentences = [
    "The weather is nice today",
    "It's a beautiful day outside",
    "I need to buy groceries",
]

embeddings = model.encode(sentences)
print(embeddings.shape)  # (3, 384) — 3 sentences, 384 dimensions
```

### Compute Similarity

```python
similarities = model.similarity(embeddings, embeddings)
print(similarities)
# tensor([[ 1.0000,  0.7235,  0.1483],
#         [ 0.7235,  1.0000,  0.1127],
#         [ 0.1483,  0.1127,  1.0000]])
```

### Run Semantic Search

```python
from sentence_transformers.util import semantic_search

corpus = [
    "Python is a programming language",
    "Java is used for enterprise applications",
    "Machine learning uses statistical methods",
    "Deep learning is a subset of machine learning",
    "The sun rises in the east",
]

corpus_embeddings = model.encode(corpus)
query_embedding = model.encode("What is deep learning?")

results = semantic_search(query_embedding, corpus_embeddings, top_k=3)
for hit in results[0]:
    print(f"Score: {hit['score']:.4f} | {corpus[hit['corpus_id']]}")
# Score: 0.7824 | Deep learning is a subset of machine learning
# Score: 0.5912 | Machine learning uses statistical methods
# Score: 0.2134 | Python is a programming language
```

### Save and Load Models

```python
# Save locally
model.save("./my_model")

# Load from local path
model = SentenceTransformer("./my_model")

# Push to Hugging Face Hub
model.push_to_hub("my-username/my-embedding-model")

# Load from Hub
model = SentenceTransformer("my-username/my-embedding-model")
```

---

## 7. Model Selection Guide

### Popular Model Families

#### General-Purpose (Original SBERT)

| Model | Dims | Max Tokens | Speed | Quality | Best For |
|-------|------|-----------|-------|---------|----------|
| `all-mpnet-base-v2` | 768 | 384 | ~2,800 sent/s | Highest (original) | Best quality general embeddings |
| `all-MiniLM-L6-v2` | 384 | 256 | ~14,200 sent/s | Good | Fast prototyping, resource-constrained |
| `all-MiniLM-L12-v2` | 384 | 256 | ~7,500 sent/s | Better | Balance of speed and quality |
| `all-distilroberta-v1` | 768 | 512 | ~4,000 sent/s | Good | Longer context needs |

#### Modern High-Performance

| Model | Dims | Max Tokens | Strengths |
|-------|------|-----------|-----------|
| `BAAI/bge-large-en-v1.5` | 1024 | 512 | Strong retrieval; needs "query: " prefix |
| `BAAI/bge-m3` | 1024 | 8192 | Multi-lingual, multi-granularity, long context |
| `intfloat/multilingual-e5-large` | 1024 | 512 | 100+ languages; needs "query: "/"passage: " prefix |
| `nomic-ai/nomic-embed-text-v1.5` | 768 | 8192 | Matryoshka-enabled, long context, open-source |
| `jinaai/jina-embeddings-v3` | 1024 | 8192 | Matryoshka, multi-task, long context |
| `Alibaba-NLP/gte-large-en-v1.5` | 1024 | 8192 | Strong retrieval performance |

#### Retrieval-Optimized (MS MARCO)

| Model | Dims | Trained On |
|-------|------|-----------|
| `multi-qa-mpnet-base-dot-v1` | 768 | 215M QA pairs |
| `multi-qa-MiniLM-L6-dot-v1` | 384 | 215M QA pairs |
| `msmarco-distilbert-base-v4` | 768 | MS MARCO passages |

### Embedding Size Tradeoffs

| Dimensions | Storage per Vector | Search Speed | Quality | Use Case |
|------------|-------------------|-------------|---------|----------|
| 128-256 | 0.5-1 KB | Fastest | Lower | Prototyping, constrained environments |
| 384 | 1.5 KB | Fast | Good | Production with speed priority |
| 768 | 3 KB | Moderate | High | Standard production |
| 1024-4096 | 4-16 KB | Slower | Highest | Maximum quality, cost secondary |

### Multilingual Models

| Model | Languages | Notes |
|-------|-----------|-------|
| `paraphrase-multilingual-mpnet-base-v2` | 50+ | General multilingual |
| `paraphrase-multilingual-MiniLM-L12-v2` | 50+ | Faster variant |
| `intfloat/multilingual-e5-large` | 100+ | Instruction-prefixed |
| `BAAI/bge-m3` | 100+ | Multi-granularity |
| `LaBSE` | 109 | Google, best for bitext mining |

### Domain-Specific Considerations

General models may underperform on specialized domains (medical, legal, scientific). Options:

1. **Fine-tune** a general model on domain-specific data (recommended)
2. **Use a domain-specific model** if available (e.g., PubMedBERT-based for biomedical)
3. **Use a large general model** (BGE-M3, GTE) — they generalize better than small models

### When to Use a Cross-Encoder Instead

Use a cross-encoder when:
- You need maximum accuracy on a small candidate set (< 1000 pairs)
- You are reranking bi-encoder results
- You need sentence-pair classification (entailment, paraphrase detection)
- Latency is acceptable (cross-encoders are 100-1000x slower)

Do NOT use a cross-encoder for:
- Initial retrieval from a large corpus
- Precomputing representations for storage
- Real-time search over millions of documents

---

## 8. Embeddings and Similarity

### How Embeddings Are Created

```
"The cat sat on the mat"
    ↓ Tokenize
[101, 1996, 4937, 2938, 2006, 1996, 13523, 102]  (token IDs)
    ↓ Transformer (12 layers of self-attention)
[[0.12, -0.45, ...], [0.33, 0.18, ...], ...]  (token embeddings, 8 × 768)
    ↓ Mean Pooling (average across non-padding tokens)
[0.22, -0.13, ...]  (sentence embedding, 1 × 768)
    ↓ Optional L2 Normalize
[0.04, -0.02, ...]  (unit-length embedding)
```

### Pooling Strategies in Detail

**Mean pooling** (recommended):
- Averages all token embeddings, excluding padding tokens
- Uses an attention mask to ignore padding
- Empirically outperforms CLS and max pooling in the original SBERT paper

**CLS token**:
- BERT prepends a special [CLS] token; its final-layer representation is used
- Without fine-tuning for sentence tasks, CLS embeddings are poor sentence representations
- Some models (like BGE) are specifically trained with CLS pooling

**Max pooling**:
- Takes the maximum value across all tokens for each dimension
- Captures the strongest activation per feature
- Rarely used in modern models

> **Warning:** Never change the pooling strategy of a pretrained model. The model was trained with a specific pooling method — using a different one degrades quality significantly.

### Cosine Similarity and Alternatives

**Cosine similarity** is the default:
```python
from sentence_transformers.util import cos_sim

similarity = cos_sim(embedding_a, embedding_b)  # range [-1, 1]
```

When embeddings are **L2-normalized** (unit length), cosine similarity equals dot product. Many models and vector databases use dot product internally for speed when embeddings are pre-normalized.

### Normalization

```python
# At encode time (recommended)
embeddings = model.encode(sentences, normalize_embeddings=True)

# Post-hoc normalization
import torch.nn.functional as F
normalized = F.normalize(torch.tensor(embeddings), p=2, dim=1)
```

Normalized embeddings:
- Make dot product equivalent to cosine similarity (faster)
- Are required by some vector databases
- Have unit length (L2 norm = 1)

### Chunking Considerations

Most models have a `max_seq_length` of 256-512 tokens. For documents longer than this:

1. **Split into chunks** of 200-400 tokens with 50-100 token overlap
2. **Encode each chunk** independently
3. **Store chunk metadata** (document_id, chunk_index, section) alongside embeddings
4. **At query time**, retrieve chunks and optionally aggregate by document

> **Warning:** Truncation loses information from the end of long texts. If key information is distributed throughout the document, chunking is essential.

### Batch Encoding Best Practices

```python
# Large corpus encoding — optimal pattern
embeddings = model.encode(
    large_corpus,
    batch_size=128,               # Adjust to GPU memory
    show_progress_bar=True,       # Set False in production
    normalize_embeddings=True,    # Pre-normalize for vector DB
    convert_to_numpy=True,        # Default; slightly faster for CPU ops
)
```

Sentence Transformers internally sorts sentences by length to minimize padding overhead within batches. This happens automatically and is reverted before returning results.

---

## 9. API and SDK Usage

### Loading Models

```python
from sentence_transformers import SentenceTransformer

# From Hugging Face Hub
model = SentenceTransformer("all-MiniLM-L6-v2")

# With specific device
model = SentenceTransformer("all-MiniLM-L6-v2", device="cuda")

# With ONNX backend (faster inference)
model = SentenceTransformer("all-MiniLM-L6-v2", backend="onnx")

# With OpenVINO backend (Intel CPU optimization)
model = SentenceTransformer("all-MiniLM-L6-v2", backend="openvino")

# With fp16 precision
model = SentenceTransformer("all-MiniLM-L6-v2", model_kwargs={"torch_dtype": "float16"})

# With Matryoshka dimension truncation
model = SentenceTransformer("nomic-ai/nomic-embed-text-v1.5", truncate_dim=256)

# With prompt templates (for instruction-prefixed models)
model = SentenceTransformer("intfloat/multilingual-e5-large", prompts={
    "query": "query: ",
    "passage": "passage: ",
})

# From local path
model = SentenceTransformer("./my_local_model")
```

### Encoding Single Sentences

```python
embedding = model.encode("Hello, world!")
print(embedding.shape)  # (384,)
print(type(embedding))  # <class 'numpy.ndarray'>
```

### Encoding Batches

```python
sentences = ["First sentence", "Second sentence", "Third sentence"]
embeddings = model.encode(sentences, batch_size=32)
print(embeddings.shape)  # (3, 384)

# Return as PyTorch tensors
embeddings = model.encode(sentences, convert_to_tensor=True)
print(type(embeddings))  # <class 'torch.Tensor'>
```

### Encoding with Prompts (Query vs. Document)

For models that require instruction prefixes (E5, BGE, Nomic):

```python
# Method 1: Use dedicated methods (v5.0+)
query_emb = model.encode_query("What is Python?")
doc_emb = model.encode_document("Python is a programming language.")

# Method 2: Use prompt_name parameter
query_emb = model.encode("What is Python?", prompt_name="query")
doc_emb = model.encode("Python is a programming language.", prompt_name="passage")

# Method 3: Manual prefix (works with any version)
query_emb = model.encode("query: What is Python?")
doc_emb = model.encode("passage: Python is a programming language.")
```

### Similarity Scoring

```python
# Using model.similarity() — respects the model's configured similarity function
similarities = model.similarity(embeddings_a, embeddings_b)

# Using utility functions directly
from sentence_transformers.util import cos_sim, dot_score

cosine_scores = cos_sim(embeddings_a, embeddings_b)
dot_scores = dot_score(embeddings_a, embeddings_b)
```

### Semantic Search

```python
from sentence_transformers.util import semantic_search

query_embedding = model.encode("search query")
corpus_embeddings = model.encode(corpus)

# Returns list of lists (one per query) of dicts with 'corpus_id' and 'score'
results = semantic_search(
    query_embedding,
    corpus_embeddings,
    top_k=10,
    score_function=cos_sim,  # or dot_score
)

for hit in results[0]:
    print(f"ID: {hit['corpus_id']}, Score: {hit['score']:.4f}")
```

### Clustering

```python
from sentence_transformers.util import community_detection
from sklearn.cluster import KMeans, AgglomerativeClustering

embeddings = model.encode(sentences)

# Community detection (graph-based, threshold-based)
clusters = community_detection(embeddings, threshold=0.75, min_community_size=5)

# K-means
kmeans = KMeans(n_clusters=5, random_state=42)
labels = kmeans.fit_predict(embeddings)

# Agglomerative clustering
agg = AgglomerativeClustering(n_clusters=None, distance_threshold=1.5)
labels = agg.fit_predict(embeddings)
```

### Saving and Loading

```python
# Save to local directory
model.save("./my_model")

# Load from local directory
model = SentenceTransformer("./my_model")

# Push to Hugging Face Hub
model.push_to_hub("my-username/my-model", private=True)
```

### ONNX Export

```python
# Load with ONNX backend (auto-exports if needed)
model = SentenceTransformer("all-MiniLM-L6-v2", backend="onnx")

# Export with optimization level
from sentence_transformers import export_optimized_onnx_model
export_optimized_onnx_model(model, "O4", "./optimized_model")

# Dynamic quantization for CPU
from sentence_transformers import export_dynamic_quantized_onnx_model
export_dynamic_quantized_onnx_model(model, "avx512_vnni", "./quantized_model")
```

---

## 10. Training and Fine-Tuning

### Training Concepts

Fine-tuning adapts a pretrained model to your specific domain or task. The general workflow:

1. **Choose a base model** (e.g., `bert-base-uncased` or `all-MiniLM-L6-v2`)
2. **Prepare training data** in the format required by your chosen loss function
3. **Select a loss function** that matches your data format
4. **Configure training** (learning rate, epochs, batch size)
5. **Add an evaluator** to monitor progress
6. **Train** with `SentenceTransformerTrainer`

### Datasets and Formats

Training data must be a Hugging Face `Dataset`. The format depends on the loss function:

| Data Format | Required Columns | Compatible Losses |
|-------------|-----------------|-------------------|
| **(anchor, positive)** pairs | 2 text columns | MultipleNegativesRankingLoss, GISTEmbedLoss |
| **(anchor, positive, negative)** triplets | 3 text columns | MultipleNegativesRankingLoss, TripletLoss |
| **(sentence_a, sentence_b, score)** | 2 text + 1 float | CoSENTLoss, CosineSimilarityLoss, AnglELoss |
| **(sentence_a, sentence_b, label)** | 2 text + 1 int | ContrastiveLoss, SoftmaxLoss |
| **(sentence,)** single text | 1 text column | DenoisingAutoEncoderLoss (unsupervised) |

```python
from datasets import Dataset

# Pair format (anchor, positive)
train_data = Dataset.from_dict({
    "anchor": ["What is Python?", "How does gravity work?"],
    "positive": ["Python is a programming language", "Gravity is a fundamental force"],
})

# Triplet format (anchor, positive, negative)
train_data = Dataset.from_dict({
    "anchor": ["What is Python?"],
    "positive": ["Python is a programming language"],
    "negative": ["Java is used for enterprise apps"],
})

# Score format (sentence_a, sentence_b, score)
train_data = Dataset.from_dict({
    "sentence1": ["The cat sat", "I love dogs"],
    "sentence2": ["A cat was sitting", "I hate cats"],
    "score": [0.9, 0.1],
})
```

### Loss Functions

#### Most Recommended

**`MultipleNegativesRankingLoss`** — The most effective loss for training top-performing models. Uses in-batch negatives (other positives in the batch serve as negatives). Requires (anchor, positive) pairs at minimum.

```python
from sentence_transformers.losses import MultipleNegativesRankingLoss

loss = MultipleNegativesRankingLoss(model)
```

**`CachedMultipleNegativesRankingLoss`** — Memory-efficient variant that enables larger effective batch sizes via gradient caching.

**`GISTEmbedLoss`** — Uses a guide model to select the best in-batch negatives. Often outperforms standard MNRL.

#### For Scored Pairs

**`CoSENTLoss`** — Recommended over `CosineSimilarityLoss`. Produces stronger gradient signal.

```python
from sentence_transformers.losses import CoSENTLoss

loss = CoSENTLoss(model)
# Dataset: (text_a, text_b, similarity_score)
```

#### Loss Modifiers

**`MatryoshkaLoss`** — Wraps any loss to train embeddings that can be truncated to smaller dimensions without notable quality loss:

```python
from sentence_transformers.losses import MatryoshkaLoss, MultipleNegativesRankingLoss

inner_loss = MultipleNegativesRankingLoss(model)
loss = MatryoshkaLoss(model, inner_loss, matryoshka_dims=[768, 512, 256, 128, 64])
```

#### Complete Loss Reference

| Category | Losses |
|----------|--------|
| **In-batch negatives** | MultipleNegativesRankingLoss, CachedMNRL, GISTEmbedLoss, CachedGIST, MegaBatchMarginLoss |
| **Scored pairs** | CoSENTLoss, AnglELoss, CosineSimilarityLoss |
| **Triplets** | TripletLoss, BatchHardTripletLoss, BatchSemiHardTripletLoss, BatchAllTripletLoss |
| **Binary labels** | ContrastiveLoss, OnlineContrastiveLoss, ContrastiveTensionLoss |
| **Wrappers** | MatryoshkaLoss, AdaptiveLayerLoss, Matryoshka2dLoss |
| **Distillation** | DistillKLDivLoss, MarginMSELoss, MSELoss |
| **Unsupervised** | DenoisingAutoEncoderLoss, SoftmaxLoss |
| **Sparse (v5.3)** | CachedSpladeLoss, GlobalOrthogonalRegularizationLoss |

### Fine-Tuning Workflow

```python
from sentence_transformers import (
    SentenceTransformer,
    SentenceTransformerTrainer,
    SentenceTransformerTrainingArguments,
)
from sentence_transformers.losses import MultipleNegativesRankingLoss
from sentence_transformers.evaluation import NanoBEIREvaluator
from datasets import load_dataset

# 1. Load base model
model = SentenceTransformer("all-MiniLM-L6-v2")

# 2. Load dataset
dataset = load_dataset("sentence-transformers/all-nli", "pair")
train_dataset = dataset["train"]
eval_dataset = dataset["validation"]

# 3. Define loss
loss = MultipleNegativesRankingLoss(model)

# 4. Define evaluator
evaluator = NanoBEIREvaluator()

# 5. Configure training
args = SentenceTransformerTrainingArguments(
    output_dir="./output",
    num_train_epochs=3,
    per_device_train_batch_size=64,
    learning_rate=2e-5,
    warmup_ratio=0.1,
    fp16=True,
    eval_strategy="steps",
    eval_steps=500,
    save_strategy="steps",
    save_steps=500,
    logging_steps=100,
)

# 6. Train
trainer = SentenceTransformerTrainer(
    model=model,
    args=args,
    train_dataset=train_dataset,
    eval_dataset=eval_dataset,
    loss=loss,
    evaluator=evaluator,
)
trainer.train()

# 7. Save
model.save_pretrained("./fine_tuned_model")
```

### Hard Negative Mining

Hard negatives are documents that are similar to the query but not relevant — they force the model to learn finer distinctions:

```python
from sentence_transformers.util import mine_hard_negatives

# Mine hard negatives from existing dataset
hard_neg_dataset = mine_hard_negatives(
    dataset=train_dataset,
    model=model,
    num_negatives=5,
    as_triplets=True,
)
```

As of v5.3.0, `MultipleNegativesRankingLoss` supports `hardness_strength` for hardness weighting of in-batch negatives.

### Evaluation During Training

```python
from sentence_transformers.evaluation import (
    EmbeddingSimilarityEvaluator,
    InformationRetrievalEvaluator,
    SequentialEvaluator,
)

# STS evaluation
sts_evaluator = EmbeddingSimilarityEvaluator(
    sentences1=eval_s1, sentences2=eval_s2, scores=eval_scores
)

# Retrieval evaluation
ir_evaluator = InformationRetrievalEvaluator(
    queries=queries, corpus=corpus, relevant_docs=qrels
)

# Combine multiple evaluators
evaluator = SequentialEvaluator([sts_evaluator, ir_evaluator])
```

### Training Tips and Pitfalls

| Tip | Details |
|-----|---------|
| **Batch size matters** | Larger batches = more in-batch negatives = better performance with MNRL. Use gradient accumulation if GPU memory is limited. |
| **Learning rate** | 2e-5 is a good starting point. Lower (1e-5) for small datasets, higher (5e-5) for large ones. |
| **Don't overtrain** | 1-3 epochs is usually sufficient. Monitor evaluation metrics for signs of overfitting. |
| **Warm up** | Use `warmup_ratio=0.1` to stabilize early training. |
| **Start from a good base** | Fine-tuning `all-MiniLM-L6-v2` is often better than training `bert-base-uncased` from scratch. |
| **Use hard negatives** | They provide a much stronger training signal than random negatives. |
| **Multi-dataset training** | Train on multiple datasets simultaneously to improve generalization. |

---

## 11. Evaluation

### STS Benchmarks

The STSbenchmark (STS-B) measures Spearman correlation between predicted cosine similarities and human-annotated similarity scores (0-5 scale).

```python
from sentence_transformers.evaluation import EmbeddingSimilarityEvaluator

evaluator = EmbeddingSimilarityEvaluator(
    sentences1=["A man is eating food"],
    sentences2=["A man is eating a piece of bread"],
    scores=[0.8],  # Normalized 0-1
)

score = evaluator(model)
print(f"STS-B Spearman: {score:.4f}")
```

### Retrieval Evaluation

```python
from sentence_transformers.evaluation import InformationRetrievalEvaluator

evaluator = InformationRetrievalEvaluator(
    queries={"q1": "What is Python?", "q2": "How to sort a list?"},
    corpus={"d1": "Python is a language", "d2": "Use sorted() to sort", "d3": "Java is compiled"},
    relevant_docs={"q1": {"d1"}, "q2": {"d2"}},
    mrr_at_k=[10],
    ndcg_at_k=[10],
    accuracy_at_k=[1, 5, 10],
    precision_recall_at_k=[10],
    map_at_k=[100],
)

metrics = evaluator(model)
```

### Retrieval Metrics Explained

| Metric | What It Measures | Good Value |
|--------|-----------------|------------|
| **MRR@k** | Average rank position of first relevant result | > 0.5 |
| **MAP@k** | Mean precision across all relevant documents | > 0.3 |
| **NDCG@k** | Ranking quality with graded relevance | > 0.4 |
| **Recall@k** | Fraction of relevant docs retrieved in top-k | > 0.8 @ k=100 |
| **Precision@k** | Fraction of top-k that are relevant | Task-dependent |

### Quick Multi-Task Evaluation

```python
from sentence_transformers.evaluation import NanoBEIREvaluator

evaluator = NanoBEIREvaluator()
score = evaluator(model)  # Runs on NanoBEIR subset — fast, no custom data needed
```

### Error Analysis Guidance

1. **Check worst-performing queries**: Find queries with lowest recall and inspect what the model retrieves instead
2. **Examine false positives**: Look at high-scoring irrelevant results — they may share surface features but differ semantically
3. **Check for domain mismatch**: If the model scores well on STS but poorly on your retrieval task, the training domain likely differs from your data
4. **Compare against BM25**: If BM25 outperforms your model, your queries may be keyword-heavy or your model needs domain fine-tuning

---

## 12. Advanced Usage

### Cross-Encoders

Cross-encoders process a text pair jointly through a single transformer, enabling full cross-attention between the two texts:

```python
from sentence_transformers import CrossEncoder

model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L6-v2")

# Score pairs
scores = model.predict([
    ("What is AI?", "Artificial intelligence is the simulation of human intelligence"),
    ("What is AI?", "The weather is nice today"),
])
print(scores)  # [8.92, -5.34] — higher = more relevant

# Rank documents for a query
results = model.rank(
    "What is machine learning?",
    [
        "ML is a subset of AI that learns from data",
        "The stock market closed higher today",
        "Deep learning uses neural networks",
    ],
    top_k=2,
)
for result in results:
    print(f"Rank: {result['corpus_id']}, Score: {result['score']:.4f}")
```

#### Popular Cross-Encoder Models

| Model | Speed | Quality | Best For |
|-------|-------|---------|----------|
| `cross-encoder/ms-marco-MiniLM-L6-v2` | Fast | Good | Production reranking |
| `cross-encoder/ms-marco-MiniLM-L12-v2` | Moderate | Better | Higher accuracy reranking |
| `BAAI/bge-reranker-base` | Moderate | High | Recommended general reranker |
| `BAAI/bge-reranker-large` | Slow | Highest | Maximum reranking quality |
| `cross-encoder/stsb-roberta-large` | Slow | High | Semantic similarity scoring |

### Reranking Pipelines

```python
from sentence_transformers import SentenceTransformer, CrossEncoder
from sentence_transformers.util import semantic_search

# Stage 1: Bi-encoder retrieval (fast)
bi_encoder = SentenceTransformer("all-MiniLM-L6-v2")
query_emb = bi_encoder.encode("What causes rain?")
corpus_emb = bi_encoder.encode(corpus)
initial_results = semantic_search(query_emb, corpus_emb, top_k=100)

# Stage 2: Cross-encoder reranking (accurate)
cross_encoder = CrossEncoder("cross-encoder/ms-marco-MiniLM-L6-v2")
candidates = [corpus[hit["corpus_id"]] for hit in initial_results[0]]
reranked = cross_encoder.rank("What causes rain?", candidates, top_k=10)
```

### Domain Adaptation

When your domain differs significantly from the model's training data:

1. **Unsupervised adaptation** (no labels):
   - TSDAE: `DenoisingAutoEncoderLoss` on domain text
   - SimCSE: Dropout-based augmentation
   - GPL: Generate synthetic queries with an LLM

2. **Supervised fine-tuning** (with labels):
   - Collect domain-specific (query, relevant_document) pairs
   - Fine-tune with `MultipleNegativesRankingLoss`
   - Add hard negatives from domain-specific candidates

### Multilingual Usage

```python
# Multilingual model — same embedding space for all languages
model = SentenceTransformer("paraphrase-multilingual-mpnet-base-v2")

embeddings = model.encode([
    "How are you?",       # English
    "Comment allez-vous?", # French
    "Wie geht es Ihnen?",  # German
    "Как дела?",           # Russian
])

# Cross-lingual similarity works automatically
similarities = model.similarity(embeddings, embeddings)
```

### Long Document Handling

For documents exceeding `max_seq_length`:

**Strategy 1: Chunking (recommended)**
```python
def chunk_text(text, chunk_size=400, overlap=100):
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size - overlap):
        chunk = " ".join(words[i:i + chunk_size])
        chunks.append(chunk)
    return chunks

chunks = chunk_text(long_document)
embeddings = model.encode(chunks)
```

**Strategy 2: Use long-context models**
```python
# Models supporting 8192 tokens
model = SentenceTransformer("nomic-ai/nomic-embed-text-v1.5")  # 8192 tokens
model = SentenceTransformer("jinaai/jina-embeddings-v3")        # 8192 tokens
```

**Strategy 3: Mean of chunk embeddings**
```python
import numpy as np

chunk_embeddings = model.encode(chunks)
document_embedding = np.mean(chunk_embeddings, axis=0)
document_embedding /= np.linalg.norm(document_embedding)  # Re-normalize
```

### ONNX and Quantization

```python
# ONNX with optimization (GPU)
model = SentenceTransformer("all-MiniLM-L6-v2", backend="onnx")

# Export optimized model
from sentence_transformers import export_optimized_onnx_model
export_optimized_onnx_model(model, "O4", "./model_onnx_o4")  # O1-O4 levels

# Dynamic quantization for CPU
from sentence_transformers import export_dynamic_quantized_onnx_model
export_dynamic_quantized_onnx_model(model, "avx512_vnni", "./model_quantized")

# OpenVINO for Intel CPUs
model = SentenceTransformer("all-MiniLM-L6-v2", backend="openvino")
```

**Performance impact (approximate):**

| Backend | Speedup (GPU) | Speedup (CPU) | Notes |
|---------|--------------|--------------|-------|
| PyTorch (baseline) | 1x | 1x | Default |
| ONNX O4 (fp16) | ~1.8x | N/A | GPU recommended |
| ONNX + int8 quantization | N/A | ~3x | CPU-specific |
| OpenVINO | N/A | ~1.3x | Intel CPUs |

### Production Inference Optimization

```python
# Optimal production encoding
model = SentenceTransformer("all-MiniLM-L6-v2", backend="onnx", device="cuda")

embeddings = model.encode(
    texts,
    batch_size=256,               # Maximize GPU utilization
    show_progress_bar=False,      # No overhead
    normalize_embeddings=True,    # Pre-normalize
    convert_to_numpy=True,        # Efficient for storage
    precision="float32",          # Or "int8" for 4x compression
)
```

---

## 13. Sentence Transformers for Search, Retrieval, and RAG

### Embedding Pipelines

```
┌──────────────────────────────────────────────────────┐
│  Ingestion Pipeline (Offline)                        │
│                                                      │
│  Documents → Chunking → Embedding Model → Vector DB  │
│              (400 tokens,  (all-MiniLM-L6-v2)        │
│               100 overlap)                           │
└──────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────┐
│  Query Pipeline (Online)                             │
│                                                      │
│  User Query → Embedding Model → Vector DB Search     │
│               (same model!)     → Top-K Candidates   │
│                                  → [Reranker]        │
│                                  → LLM Context       │
└──────────────────────────────────────────────────────┘
```

> **Warning:** You must use the same embedding model for both ingestion and querying. Embeddings from different models are incompatible.

### Chunking Strategy Implications

| Strategy | Chunk Size | Overlap | Pros | Cons |
|----------|-----------|---------|------|------|
| Fixed-size | 300-500 tokens | 50-100 | Simple, predictable | May split mid-sentence |
| Sentence-based | Natural sentences | 1-2 sentences | Preserves meaning | Variable chunk sizes |
| Paragraph-based | Natural paragraphs | 0-1 paragraph | Preserves context | Large, uneven chunks |
| Recursive/semantic | Variable | Variable | Best quality | Most complex |

**Practical recommendation:** Start with 400-token chunks with 100-token overlap. Adjust based on retrieval evaluation metrics.

### Vector Database Integration Patterns

```python
# === Qdrant ===
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance

client = QdrantClient("localhost", port=6333)
client.create_collection("docs", vectors_config=VectorParams(size=384, distance=Distance.COSINE))

embeddings = model.encode(texts, normalize_embeddings=True)
points = [PointStruct(id=i, vector=emb.tolist(), payload={"text": text})
          for i, (emb, text) in enumerate(zip(embeddings, texts))]
client.upsert("docs", points=points)

# Search
query_emb = model.encode("search query", normalize_embeddings=True)
results = client.query_points("docs", query=query_emb.tolist(), limit=10)
```

```python
# === Chroma ===
import chromadb

client = chromadb.Client()
collection = client.create_collection("docs")

embeddings = model.encode(texts).tolist()
collection.add(ids=[str(i) for i in range(len(texts))], embeddings=embeddings, documents=texts)

query_emb = model.encode("search query").tolist()
results = collection.query(query_embeddings=[query_emb], n_results=10)
```

```python
# === FAISS (pure similarity search) ===
import faiss
import numpy as np

embeddings = model.encode(texts, normalize_embeddings=True).astype("float32")
index = faiss.IndexFlatIP(embeddings.shape[1])  # Inner product (= cosine for normalized vectors)
index.add(embeddings)

query_emb = model.encode("search query", normalize_embeddings=True).astype("float32").reshape(1, -1)
scores, indices = index.search(query_emb, k=10)
```

### Hybrid Retrieval Patterns

Combine dense (semantic) and sparse (keyword) retrieval:

```python
from sentence_transformers import SentenceTransformer, SparseEncoder

# Dense embeddings
dense_model = SentenceTransformer("all-MiniLM-L6-v2")
dense_emb = dense_model.encode(texts)

# Sparse embeddings (SPLADE)
sparse_model = SparseEncoder("naver/splade-cocondenser-ensembledistil")
sparse_emb = sparse_model.encode(texts)

# Store both in a vector DB that supports hybrid search (e.g., Qdrant, Weaviate)
# At query time, search with both and fuse results (RRF or score normalization)
```

### Reranking Placement

```
User Query
    ↓
Embedding Model (query → vector)
    ↓
Vector Database (top-100 by similarity)
    ↓
Cross-Encoder Reranker (top-100 → top-10)     ← HERE
    ↓
LLM (generate answer with top-10 as context)
```

### Metadata Handling Strategies

Store metadata alongside embeddings for filtering:

```python
# Per chunk metadata
metadata = {
    "document_id": "doc-123",
    "chunk_index": 5,
    "text": chunk_text,
    "source": "user_manual.pdf",
    "page": 12,
    "section": "Installation",
    "created_at": "2025-06-01",
    "access_level": "public",
}
```

Filter at query time:
```python
# Qdrant example: filter by source and date
from qdrant_client.models import Filter, FieldCondition, MatchValue, Range

results = client.query_points(
    "docs",
    query=query_emb.tolist(),
    query_filter=Filter(must=[
        FieldCondition(key="source", match=MatchValue(value="user_manual.pdf")),
        FieldCondition(key="created_at", range=Range(gte="2025-01-01")),
    ]),
    limit=10,
)
```

### End-to-End RAG Example Architecture

```
┌─────────────────────────────────────────────────────────┐
│  User Question: "How do I configure logging?"           │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│  Embedding Model: all-MiniLM-L6-v2                      │
│  Query → [0.12, -0.45, 0.33, ...] (384 dims)           │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│  Vector Database (Qdrant)                                │
│  Collection: "documentation"                             │
│  Filter: source = "user_guide"                          │
│  → Top 50 chunks by cosine similarity                   │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│  Cross-Encoder: cross-encoder/ms-marco-MiniLM-L6-v2     │
│  Rerank 50 chunks → Top 5                               │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│  LLM (Claude / GPT)                                      │
│  System: "Answer based on the provided context"          │
│  Context: [top 5 chunks with metadata]                   │
│  User: "How do I configure logging?"                     │
│  → Generated answer with citations                       │
└──────────────────────────────────────────────────────────┘
```

---

## 14. Deployment and Production Guidance

### Local Applications

For desktop apps, CLI tools, or local scripts:

```python
model = SentenceTransformer("all-MiniLM-L6-v2", device="cpu")
# Or with ONNX for faster CPU inference:
model = SentenceTransformer("all-MiniLM-L6-v2", backend="onnx")
```

### Batch Inference Jobs

```python
import numpy as np

model = SentenceTransformer("all-MiniLM-L6-v2", device="cuda")

# Process large corpus in batches
all_embeddings = model.encode(
    corpus,
    batch_size=256,
    show_progress_bar=True,
    normalize_embeddings=True,
)

# Save to disk
np.save("embeddings.npy", all_embeddings)
```

### API Serving with FastAPI

```python
from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

app = FastAPI()
model = SentenceTransformer("all-MiniLM-L6-v2", device="cuda")

class EncodeRequest(BaseModel):
    texts: list[str]
    normalize: bool = True

class EncodeResponse(BaseModel):
    embeddings: list[list[float]]
    dimensions: int

@app.post("/encode", response_model=EncodeResponse)
async def encode(request: EncodeRequest):
    embeddings = model.encode(
        request.texts,
        normalize_embeddings=request.normalize,
        convert_to_numpy=True,
    )
    return EncodeResponse(
        embeddings=embeddings.tolist(),
        dimensions=embeddings.shape[1],
    )

@app.get("/health")
async def health():
    return {"status": "ok", "model": model.get_sentence_embedding_dimension()}
```

> **Note:** FastAPI with a single model instance is not the most performant for high-throughput production. Consider dedicated inference servers for >100 QPS.

### Dedicated Inference Servers

| Server | Speedup | Features |
|--------|---------|----------|
| **HF Text Embeddings Inference (TEI)** | 2-4x | Production batching, metrics, Docker-ready |
| **NVIDIA Triton + TensorRT** | 5-10x | Maximum GPU throughput, dynamic batching |
| **ONNX Runtime Server** | 2-3x | CPU-optimized, cross-platform |

### Throughput and Latency Tuning

| Factor | Impact on Throughput | Impact on Latency |
|--------|---------------------|-------------------|
| Batch size ↑ | ↑ (better GPU utilization) | ↑ (more to process per batch) |
| Model size ↓ | ↑ (faster inference) | ↓ (faster per-item) |
| ONNX backend | ↑ (1.8-3x) | ↓ |
| fp16/bf16 | ↑ (~1.5x) | ↓ |
| GPU vs CPU | ↑ (5-50x) | ↓↓ |
| Embedding dims ↓ | Minor impact | Minor impact on encode; major on search |

### CPU vs. GPU Considerations

| Scenario | Recommendation |
|----------|---------------|
| < 10 QPS, cost-sensitive | CPU with ONNX int8 quantization |
| 10-100 QPS | Single GPU (T4 or better) |
| 100-1000 QPS | GPU + TEI/Triton with dynamic batching |
| > 1000 QPS | Multiple GPUs + load balancer |
| Batch processing (millions) | GPU with large batch sizes |

### Monitoring

Track these metrics in production:

- **Encoding latency** (p50, p95, p99)
- **Throughput** (embeddings/second)
- **Queue depth** (pending requests)
- **GPU utilization** and memory
- **Model load time** on cold start
- **Error rate** (OOM, timeout, invalid input)

### Model Versioning

- **Pin model versions** in production (e.g., `sentence-transformers/all-MiniLM-L6-v2` — the Hub version is immutable)
- **Re-index when changing models** — embeddings from different models are incompatible
- **Use model cards** to document which model produced which embeddings
- **Store model version** alongside embeddings in your vector database metadata

---

## 15. Performance Optimization

### Batch Size Tuning

| Hardware | Recommended Batch Size | Notes |
|----------|----------------------|-------|
| CPU | 8-16 | Limited by memory bandwidth |
| GPU (8 GB VRAM) | 32-64 | Depends on model size and seq length |
| GPU (16 GB VRAM) | 64-128 | |
| GPU (24+ GB VRAM) | 128-512 | Larger = better utilization |

Start with 32 (default), increase until you see OOM errors, then back off by 25%.

### Precision and Memory Tradeoffs

| Precision | Memory per Vector (384-dim) | Quality | Speed |
|-----------|---------------------------|---------|-------|
| float32 | 1,536 bytes | Best | Baseline |
| float16 | 768 bytes | Near-identical | ~1.5x faster |
| int8 | 384 bytes | Minor loss | Varies |
| binary | 48 bytes | Noticeable loss | Much faster search |

```python
# Embedding-level quantization
emb_f32 = model.encode(texts, precision="float32")   # 1,536 bytes/vec
emb_int8 = model.encode(texts, precision="int8")      # 384 bytes/vec
emb_bin = model.encode(texts, precision="binary")      # 48 bytes/vec
```

### Caching

- **Precompute corpus embeddings** — encode once, store in vector DB or numpy files
- **Cache query embeddings** for repeated queries (LRU cache)
- **Cache model loading** — keep model in memory across requests

```python
from functools import lru_cache

@lru_cache(maxsize=10000)
def get_query_embedding(query: str) -> tuple:
    return tuple(model.encode(query, normalize_embeddings=True).tolist())
```

### Multi-Process Encoding

For multi-GPU encoding:

```python
pool = model.start_multi_process_pool(target_devices=["cuda:0", "cuda:1", "cuda:2"])
embeddings = model.encode(large_corpus, pool=pool, batch_size=128)
model.stop_multi_process_pool(pool)
```

### Embedding Storage Considerations

| 1M documents | float32 (384-d) | int8 (384-d) | binary (384-d) |
|-------------|-----------------|-------------|----------------|
| **Storage** | ~1.5 GB | ~384 MB | ~48 MB |
| **RAM (in-memory)** | ~1.5 GB | ~384 MB | ~48 MB |

For large-scale deployments, consider:
- Matryoshka dimension reduction (768 → 256): ~3x savings
- int8 quantization: ~4x savings
- Combined: ~12x savings

### Benchmarking Guidance

1. Measure encoding throughput (sentences/second) at different batch sizes
2. Measure query latency (ms) end-to-end including vector DB search
3. Measure recall@k against a ground truth dataset
4. Compare models on **your** data, not just MTEB — domain matters
5. Test with representative query lengths and complexity

---

## 16. Common Pitfalls

### Wrong Model Choice

| Mistake | Consequence | Fix |
|---------|-------------|-----|
| Using STS model for retrieval | Poor retrieval quality | Use retrieval-trained model (multi-qa, BGE, E5) |
| Using English model for multilingual data | Garbage embeddings for non-English text | Use multilingual model |
| Using huge model when speed matters | Unacceptable latency | Use MiniLM or quantized model |
| Using general model for specialized domain | Poor domain-specific quality | Fine-tune or use domain model |

### Misusing Cosine Similarity

- **Pitfall**: Comparing embeddings from different models — they live in different vector spaces
- **Pitfall**: Expecting cosine similarity to be calibrated (0.8 doesn't universally mean "highly similar")
- **Fix**: Always compare embeddings from the same model. Use relative ranking, not absolute thresholds.

### Poor Chunking

- **Too small** (50-100 tokens): Loses context, retrieves noise
- **Too large** (1000+ tokens): Exceeds max_seq_length, gets truncated, dilutes relevance
- **No overlap**: Misses information at chunk boundaries
- **No metadata**: Cannot trace results back to source documents

### Unnormalized Embeddings

- **Pitfall**: Using dot product on unnormalized embeddings — magnitude dominates, not direction
- **Fix**: Always use `normalize_embeddings=True` when using dot product for similarity, or use cosine similarity directly

### Incorrect Evaluation Setup

- **Pitfall**: Evaluating retrieval quality with STS metrics (or vice versa)
- **Pitfall**: Using the same data for training and evaluation
- **Pitfall**: Not testing on domain-specific data
- **Fix**: Match your evaluator to your task. Use InformationRetrievalEvaluator for retrieval, EmbeddingSimilarityEvaluator for STS.

### Overfitting During Fine-Tuning

- **Signs**: Training loss decreases but evaluation metrics plateau or degrade
- **Causes**: Too many epochs, too small dataset, no hard negatives
- **Fix**: Train for 1-3 epochs, monitor evaluation loss, use early stopping, increase data diversity

### Confusing Cross-Encoders and Bi-Encoders

| What People Do | What Happens | What To Do Instead |
|----------------|-------------|-------------------|
| Use cross-encoder to encode a whole corpus | Takes hours/days, no precomputed embeddings | Use bi-encoder for corpus encoding |
| Use bi-encoder for final ranking | Suboptimal accuracy | Add cross-encoder reranking stage |
| Store cross-encoder "embeddings" | Cross-encoders don't produce embeddings | Use bi-encoder for vector storage |

---

## 17. Comparison and Tradeoffs

### Sentence Transformers vs. Raw Hugging Face Transformers

| Aspect | Sentence Transformers | Raw HF Transformers |
|--------|----------------------|---------------------|
| **Sentence embedding** | `.encode()` — one line | Manual: tokenize → forward → pool → normalize |
| **Similarity** | `.similarity()` — built-in | Manual: compute cosine/dot yourself |
| **Training** | SentenceTransformerTrainer + 30 losses | Write custom training loop |
| **Evaluation** | 8+ built-in evaluators | Write custom evaluation code |
| **Model hub** | 15,000+ pretrained embedding models | Same hub, but may need pooling config |
| **Overhead** | Minimal (thin wrapper) | None |
| **Flexibility** | High for embedding tasks | Maximum for any task |

**Use Sentence Transformers** for any embedding task. Use raw Transformers only for token-level tasks (NER, POS tagging) or when you need full control over the architecture.

### Bi-Encoders vs. Cross-Encoders (Detailed)

| Factor | Bi-Encoder | Cross-Encoder |
|--------|-----------|---------------|
| **Encoding** | Independent per text | Joint per pair |
| **Precomputable** | Yes | No |
| **Speed (1000 docs)** | ~0.1s encode + instant compare | ~10s (1000 forward passes) |
| **Accuracy** | Good (95-97% of cross-encoder) | Best |
| **Storage** | Embeddings in vector DB | No storage (compute on demand) |
| **Scale** | Billions of documents | Hundreds of candidates |
| **API** | `SentenceTransformer` | `CrossEncoder` |

### Dense Retrieval vs. Sparse Retrieval

| Factor | Dense (Sentence Transformers) | Sparse (BM25 / SPLADE) |
|--------|------------------------------|----------------------|
| **Captures** | Semantic meaning, paraphrases | Exact keyword matches |
| **Fails at** | Rare proper nouns, exact terms | Synonyms, paraphrases |
| **Model needed** | Yes (neural network) | No (BM25) or yes (SPLADE) |
| **Vector size** | 384-4096 floats | Vocabulary-sized sparse |
| **Best alone?** | For semantic queries | For keyword queries |
| **Best together?** | Yes — hybrid retrieval (dense + sparse + fusion) |

### When Another Approach May Be Better

| Need | Better Alternative |
|------|--------------------|
| Exact keyword search | Elasticsearch, Meilisearch |
| Token-level tasks (NER, POS) | Raw HF Transformers |
| Very large LLM embeddings (GPT, Claude) | API-based embeddings (no local model) |
| Sub-millisecond latency | Precomputed lookup tables, BM25 |
| Zero-shot classification | NLI models or LLMs directly |
| Image-only search | CLIP or dedicated vision models |

---

## 18. Best Practices Checklist

- [ ] **Use the same model** for encoding queries and documents — never mix models
- [ ] **Normalize embeddings** (`normalize_embeddings=True`) when using dot product similarity
- [ ] **Create payload indexes** on filter fields in your vector database
- [ ] **Chunk long documents** into 300-500 tokens with 50-100 overlap
- [ ] **Store metadata** (document_id, source, section) alongside embeddings
- [ ] **Use a retrieval-trained model** (BGE, E5, multi-qa) for search tasks, not an STS model
- [ ] **Add cross-encoder reranking** for production search (bi-encoder top-100 → reranker top-10)
- [ ] **Fine-tune on domain data** if general models underperform on your domain
- [ ] **Use `MultipleNegativesRankingLoss`** with hard negatives for fine-tuning
- [ ] **Wrap with `MatryoshkaLoss`** during training for dimension flexibility
- [ ] **Use ONNX backend** for production inference (~2-3x speedup)
- [ ] **Batch encode with appropriate batch sizes** (32-256 depending on GPU)
- [ ] **Set `show_progress_bar=False`** in production
- [ ] **Pin model versions** and re-index when changing models
- [ ] **Evaluate on your task** — don't rely solely on MTEB scores
- [ ] **Monitor latency and throughput** in production
- [ ] **Use multi-process encoding** for multi-GPU batch jobs
- [ ] **Use appropriate prompt prefixes** for models that require them (E5: "query:", BGE: "Represent this...")
- [ ] **Start with a pretrained model** — don't train from scratch unless you have millions of labeled pairs
- [ ] **Test chunking strategies** empirically — there is no universally best chunk size

---

## 19. Quick Start Recap

```bash
# Install
pip install -U sentence-transformers
```

```python
from sentence_transformers import SentenceTransformer
from sentence_transformers.util import semantic_search, cos_sim

# 1. Load model
model = SentenceTransformer("all-MiniLM-L6-v2")

# 2. Encode texts
corpus = ["Python is a language", "Java compiles to bytecode", "Rain falls from clouds"]
corpus_emb = model.encode(corpus, normalize_embeddings=True)

# 3. Search
query_emb = model.encode("What is Python?", normalize_embeddings=True)
results = semantic_search(query_emb, corpus_emb, top_k=3)
for hit in results[0]:
    print(f"{hit['score']:.4f}: {corpus[hit['corpus_id']]}")

# 4. Similarity matrix
sims = model.similarity(corpus_emb, corpus_emb)
print(sims)

# 5. Rerank with cross-encoder
from sentence_transformers import CrossEncoder
reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L6-v2")
ranked = reranker.rank("What is Python?", corpus)
for r in ranked:
    print(f"Score: {r['score']:.4f} | {corpus[r['corpus_id']]}")

# 6. Save model
model.save("./my_model")
```

---

## 20. References

### Official Documentation

- [Sentence Transformers Documentation](https://sbert.net/) — Official docs site
- [SentenceTransformer API Reference](https://sbert.net/docs/package_reference/sentence_transformer/SentenceTransformer.html) — Class reference
- [Loss Overview](https://sbert.net/docs/sentence_transformer/loss_overview.html) — All loss functions
- [Pretrained Models](https://www.sbert.net/docs/sentence_transformer/pretrained_models.html) — Model catalog
- [Cross-Encoder Usage](https://sbert.net/docs/cross_encoder/usage/usage.html) — Cross-encoder guide
- [Training Overview](https://sbert.net/docs/sentence_transformer/training_overview.html) — Training pipeline
- [Efficiency Guide](https://sbert.net/docs/sentence_transformer/usage/efficiency.html) — ONNX, quantization, speedup

### Repositories

- [Sentence Transformers GitHub](https://github.com/UKPLab/sentence-transformers) — Source code
- [MTEB Benchmark](https://github.com/embeddings-benchmark/mteb) — Evaluation benchmark
- [MTEB Leaderboard](https://huggingface.co/spaces/mteb/leaderboard) — Current model rankings

### Key Papers

- Reimers & Gurevych (2019) — *Sentence-BERT: Sentence Embeddings using Siamese BERT-Networks* — The foundational SBERT paper
- Reimers & Gurevych (2020) — *Making Monolingual Sentence Embeddings Multilingual using Knowledge Distillation*
- Gao et al. (2021) — *SimCSE: Simple Contrastive Learning of Sentence Embeddings*
- Wang et al. (2022) — *Text Embeddings by Weakly-Supervised Contrastive Pre-training* (E5)
- Xiao et al. (2023) — *C-Pack: Packaged Resources To Advance General Chinese Embedding* (BGE)
- Kusupati et al. (2022) — *Matryoshka Representation Learning*
- Muennighoff et al. (2023) — *MTEB: Massive Text Embedding Benchmark*

### Useful Resources

- [Train Sentence Transformers v3 (HF Blog)](https://huggingface.co/blog/train-sentence-transformers) — Training tutorial
- [Train Reranker v4 (HF Blog)](https://huggingface.co/blog/train-reranker) — Cross-encoder training
- [Matryoshka Embeddings Guide](https://sbert.net/examples/sentence_transformer/training/matryoshka/README.html) — Variable-dimension training
- [Computing Embeddings Guide](https://sbert.net/examples/sentence_transformer/applications/computing-embeddings/README.html) — Batch processing optimization
- [Sentence Transformers Releases](https://github.com/UKPLab/sentence-transformers/releases) — Version changelog

---

## Appendix A: Feature Summary

| Category | Features |
|----------|----------|
| **Model Types** | SentenceTransformer (dense), CrossEncoder (reranker), SparseEncoder (SPLADE, v5.0+) |
| **Embeddings** | Dense, sparse, Matryoshka (variable-dim), quantized (int8, uint8, binary, ubinary) |
| **Pooling** | Mean (default), CLS, max, weighted mean |
| **Similarity** | Cosine, dot product, Euclidean, Manhattan |
| **Tasks** | Semantic similarity, search, clustering, paraphrase mining, classification, reranking |
| **Training** | 30+ losses, SentenceTransformerTrainer, CrossEncoderTrainer, multi-dataset, hard negatives |
| **Evaluation** | EmbeddingSimilarity, InformationRetrieval, NanoBEIR, Triplet, Reranking, 8+ evaluators |
| **Optimization** | ONNX (O1-O4), OpenVINO, fp16/bf16, int8 quantization, multi-process encoding |
| **Languages** | 100+ (multilingual models), cross-lingual transfer |
| **Integrations** | LangChain, LlamaIndex, Qdrant, Pinecone, Chroma, Weaviate, Milvus, pgvector, FAISS |

## Appendix B: Recommended Learning Path

1. **Day 1 — Quick Start**: Install, load `all-MiniLM-L6-v2`, encode sentences, compute similarity. Run `semantic_search` on a small corpus.
2. **Day 2 — Model Exploration**: Try different models (all-mpnet, BGE, E5). Compare results on your data. Understand prompt prefixes.
3. **Day 3 — Vector Database**: Store embeddings in Qdrant or Chroma. Build a simple search API.
4. **Day 4 — Cross-Encoders**: Load a cross-encoder. Build a bi-encoder → cross-encoder reranking pipeline.
5. **Day 5 — Evaluation**: Run NanoBEIREvaluator. Set up InformationRetrievalEvaluator on your data.
6. **Week 2 — Fine-Tuning**: Prepare training data. Fine-tune with MultipleNegativesRankingLoss. Compare before/after.
7. **Week 3 — Production**: Export to ONNX. Set up FastAPI or TEI. Benchmark throughput and latency.
8. **Week 4 — Advanced**: Try hybrid search (dense + sparse). Implement Matryoshka training. Mine hard negatives.
9. **Ongoing**: Monitor production metrics. Re-evaluate when new models appear on MTEB. Fine-tune periodically on fresh data.

## Appendix C: Top 10 Implementation Tips

1. **Start with `all-MiniLM-L6-v2`** for prototyping — it's fast, good quality, and well-tested. Upgrade to BGE-M3 or E5-large for production.
2. **Always normalize embeddings** when using dot product or storing in vector databases that default to inner product.
3. **Use the same model for queries and documents** — embedding spaces are model-specific and incompatible across models.
4. **Add cross-encoder reranking** — it consistently improves retrieval quality by 5-15% with minimal latency overhead on small candidate sets.
5. **Fine-tune with `MultipleNegativesRankingLoss`** — it's the most effective loss for training embedding models. Add hard negatives for stronger signal.
6. **Use `MatryoshkaLoss`** during training — it gives you dimension flexibility at no quality cost, enabling storage/speed tradeoffs at inference time.
7. **Export to ONNX for production** — 1.8-3x speedup is free performance. Use O4 for GPU, int8 quantization for CPU.
8. **Chunk documents at 300-500 tokens with overlap** — this fits within most models' max_seq_length and preserves context. Always store chunk metadata.
9. **Evaluate on your own data** — MTEB scores are useful for model shortlisting, but your domain-specific evaluation determines the real winner.
10. **Monitor and version models** — pin specific model versions, track embedding quality over time, and re-index when switching models.

---

*Guide version: 1.0 — March 2026*
*Covers Sentence Transformers v5.3.x*
*Sources: Official documentation (sbert.net), GitHub repository, Hugging Face Hub, and referenced papers.*

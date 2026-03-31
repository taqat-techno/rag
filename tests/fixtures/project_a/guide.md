# Developer Guide

Quick reference for common development tasks.

## Getting Started

Clone the repository and install dependencies:

```bash
git clone https://github.com/example/project-alpha.git
cd project-alpha
pip install -e ".[dev]"
```

Run the development server:

```bash
python manage.py runserver
```

## Testing

Run all tests with pytest:

```bash
pytest -v
```

Run a single test file:

```bash
pytest tests/test_auth.py -v
```

## Code Style

We use Black for formatting and Ruff for linting.
All code must pass CI checks before merge.

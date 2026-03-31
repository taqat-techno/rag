---
title: Project Alpha
version: "1.0"
---

# Project Alpha

This is the main documentation for Project Alpha.

## Architecture

The system uses a microservices architecture with three main components.

### Backend

The backend is built with Python and FastAPI. It handles all business logic
and data processing. The API serves REST endpoints for the frontend.

Authentication uses JWT tokens with refresh token rotation.
Sessions are stored in Redis for fast access.

### Frontend

The frontend is a Next.js application with React components.
It communicates with the backend via REST API calls.

State management uses Zustand for global state and TanStack Query
for server state.

### Database

PostgreSQL is the primary database. We use SQLAlchemy as the ORM.
Migrations are managed with Alembic.

The schema includes tables for users, projects, tasks, and audit logs.
Each table uses UUID primary keys.

## API Reference

### Authentication Endpoints

POST /api/auth/login — authenticate and receive tokens.
POST /api/auth/refresh — refresh an expired access token.
POST /api/auth/logout — invalidate current session.

### User Endpoints

GET /api/users — list all users (admin only).
GET /api/users/:id — get user details.
PUT /api/users/:id — update user profile.

## Deployment

The application is deployed using Docker Compose.
Production runs on AWS ECS with auto-scaling enabled.

CI/CD pipeline uses GitHub Actions with separate staging and production environments.

-- Runs once as the postgres superuser when the container volume is first created.
-- The application connects as a NON-superuser so PostgreSQL Row-Level Security applies.
CREATE ROLE tendersync_app LOGIN PASSWORD 'tendersync_app' NOSUPERUSER NOBYPASSRLS;
CREATE DATABASE tendersync OWNER tendersync_app;
\connect tendersync
CREATE EXTENSION IF NOT EXISTS vector;
GRANT ALL ON SCHEMA public TO tendersync_app;

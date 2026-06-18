-- Enable pgvector in the databases the stack uses.
-- Runs once on first Postgres container init.

-- Gateway DB (Sequelize tables; vector not strictly needed but harmless).
CREATE EXTENSION IF NOT EXISTS vector;

-- SOPs Chat Bot stores its embeddings (langchain-postgres PGVector) in the
-- default "postgres" database.
\connect postgres
CREATE EXTENSION IF NOT EXISTS vector;

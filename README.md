# 7Sense CM Cloud v1.9.1

Correção do driver PostgreSQL para Supabase/Render usando `psycopg[binary]`.

## Atualização
1. Copie os arquivos desta pasta para o repositório `7sense-cm-cloud`.
2. Commit: `v1.9.1 corrigir driver PostgreSQL`
3. Push origin.
4. No Render, faça `Clear build cache & deploy`.

## Variável Render
`DATABASE_URL` deve apontar para a URI PostgreSQL do Supabase.

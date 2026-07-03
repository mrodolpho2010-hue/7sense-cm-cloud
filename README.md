# 7Sense CM Cloud v1.9.2

Versão corrigida para PostgreSQL/Supabase via Session Pooler.

## Inclui
- Driver `psycopg[binary]==3.2.13`
- Correção da tabela `camera_history` com campo `install_photo`
- Foto de instalação opcional no campo
- Visão hierárquica Cliente > Obra > Câmeras
- Mantém banco permanente Supabase/PostgreSQL

## Atualização
1. Copie os arquivos desta pasta para o repositório `7sense-cm-cloud`.
2. Commit: `v1.9.2 Supabase e foto instalacao`
3. Push origin.
4. No Render, use `Manual Deploy > Clear build cache & deploy`.

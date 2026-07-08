# 7Sense CM Cloud - v3.1.10

## Otimização do Dashboard

Correções principais:

- Dashboard otimizado para evitar timeout no Render/Supabase.
- Contagens de câmeras por status agora usam consulta agrupada.
- Mantém alertas de vencimento de contratos da v3.1.9.
- Reduz quantidade de conexões e consultas ao banco na tela inicial.

## Deploy

1. Copie os arquivos para a pasta do GitHub.
2. Commit: `v3.1.10 otimizacao dashboard`
3. Push origin.
4. Render: Manual Deploy > Clear build cache & deploy.

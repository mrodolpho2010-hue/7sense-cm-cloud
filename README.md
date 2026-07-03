# 7Sense CM Cloud v1.9.7

Correção do modo demonstração:

- Sistema abre sempre no modo operação oficial.
- Entrar no modo demonstração não limpa o banco oficial.
- Dados de demonstração usam `demo=1` e códigos `7S-DEMO-CAM-900+`.
- Botão "Voltar ao modo operação" apenas retorna ao ambiente oficial.
- QR Codes de demonstração não conflitam com QR Codes reais.
- Evita travamento/erro no endpoint `/demo/load` no PostgreSQL/Supabase.

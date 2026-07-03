# 7Sense – Data into Action | Contract Manager v1.8

Versão com suporte a banco permanente PostgreSQL/Supabase.

## O que mudou

- Se existir a variável `DATABASE_URL`, o sistema usa PostgreSQL/Supabase.
- Se `DATABASE_URL` não existir, continua usando SQLite local.
- Mantém o fluxo de campo, QR Code, histórico, clientes, contratos e câmeras.

## Render

Configure em Environment:

```text
DATABASE_URL=postgresql://postgres:SUA_SENHA@db.kegkupvivmjxedvmvvwx.supabase.co:5432/postgres
```

O sistema adiciona `sslmode=require` automaticamente quando necessário.

## Login inicial

Operação:

```text
marcos@7sense.local
123456
```

Diretoria:

```text
diretoria@7sense.local
123456
```

## Teste recomendado

1. Faça commit e push desta versão.
2. Aguarde o deploy no Render.
3. Abra `/login`.
4. Cadastre uma câmera nova, por exemplo `7S-CAM-300`.
5. Leia o QR ou digite o código em `/campo`.
6. Altere o status.
7. Aguarde o Render reiniciar ou feche tudo.
8. Abra novamente e confirme que a câmera continua cadastrada.

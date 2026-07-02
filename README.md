# 7Sense – Data into Action | Contract Manager Cloud

Versão cloud inicial para homologação no Render.

## Rodar localmente

```bash
pip install -r requirements.txt
python app.py
```

Abra: http://127.0.0.1:5000

## Acessos

Operação:
- usuário: `marcos@7sense.local`
- senha: `123456`

Diretoria:
- usuário: `diretoria@7sense.local`
- senha: `123456`

## Render

Build Command:
```bash
pip install -r requirements.txt
```

Start Command:
```bash
gunicorn app:app
```

## Links

- Painel: `/app`
- Campo público: `/campo`

O leitor de QR Code no celular requer HTTPS. No Render isso já vem habilitado.

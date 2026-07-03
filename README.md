# 7Sense – Data into Action | Contract Manager v1.9

## O que entrou nesta versão

### 1. Foto da instalação no 7Sense Campo
- Após a etapa **🟢 Ativar câmera**, aparece o campo **📸 Foto da instalação (opcional)**.
- A foto é anexada ao histórico da câmera.
- A foto também aparece na ficha da câmera no painel do computador.
- O fluxo continua simples: a foto é opcional e não bloqueia a ativação.

### 2. Câmeras organizadas por hierarquia
Na aba **Câmeras**, a visualização agora fica assim:

Status/filtro selecionado → Cliente → Obra/Contrato → Câmeras

Exemplo:

- Toyota
  - Sorocaba/SP
    - 7S-CAM-001
    - 7S-CAM-002
- Equinix
  - Tamboré/SP
    - 7S-CAM-010

Isso facilita saber primeiro de qual cliente e obra são as câmeras.

### 3. Banco permanente mantido
- Continua usando `DATABASE_URL` no Render para conectar ao Supabase/PostgreSQL.
- Se `DATABASE_URL` não existir, roda localmente com SQLite.

## Como atualizar no Render

1. Extraia este ZIP.
2. Copie os arquivos de dentro da pasta `7sense-cm-cloud-v1_9`.
3. Cole na pasta do GitHub `7sense-cm-cloud`, substituindo os arquivos.
4. No GitHub Desktop:
   - Summary: `v1.9 foto instalacao e cameras por cliente obra`
   - Commit to main
   - Push origin
5. Aguarde o Render atualizar automaticamente.

## Teste recomendado

1. No celular, acesse `/campo`.
2. Leia uma câmera que esteja na etapa **Instalando**.
3. Clique em **Ativar câmera**.
4. Tire ou escolha uma foto da instalação.
5. Salve.
6. No painel do computador, abra a ficha da câmera e confira se a foto apareceu no histórico.

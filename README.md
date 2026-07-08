# 7Sense Operations Manager v3.1.8

## Vinculação de câmeras aos contratos

Alterações principais:
- Contratos não abrem mais cadastro de câmera nova.
- Botão no contrato mudou para **Vincular câmera**.
- Vinculação lista somente câmeras já cadastradas, testadas e aprovadas, sem vínculo com outra obra.
- Controle de limite pela quantidade de câmeras prevista no contrato.
- Status novo: **Reservada**.
- Câmera reservada pode ser desvinculada antes de ir para transporte.
- Dossiê registra reserva e cancelamento de reserva.

Fluxo correto:
Cadastrar câmera > Testar/aprovar > Vincular ao contrato > Reservada > Enviar para campo/transporte.


## v3.1.8
- Correção da vinculação de câmeras no PostgreSQL/Supabase.
- Removida comparação inválida contract_id='' em campo inteiro.
- A lista de vínculo exibe somente câmeras testadas/aprovadas e sem contrato.

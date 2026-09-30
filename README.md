# Meta Ads Control Center

Dashboard automático das contas de anúncio Meta (Digiê, Energius [Whats], Energius [Forms]), atualizado todo dia às 8h (horário de Brasília) via GitHub Actions e publicado no GitHub Pages.

## Como funciona

1. `.github/workflows/daily.yml` roda todo dia às 11:00 UTC (8h BRT), ou pode ser disparado manualmente na aba **Actions → Meta Ads Control Center → Run workflow**.
2. `fetch_and_build.py` busca os dados direto na **Meta Graph API** (Marketing API) para as 3 contas de anúncio, reproduzindo a mesma lógica usada antes com o Windsor.ai:
   - Série diária de investimento/leads desde 01/01/2026.
   - Segmentação: `forms` (Energius [Forms]), `whats` (Energius [Whats], usa `onsite_conversion.messaging_conversation_started_7d`), `dg` e `dg_parceiros` (Digiê, dividida pela campanha `DIGIÊ [PARCEIROS]`).
   - Regra de saldo crítico com projeção de gasto considerando fins de semana.
   - Ranking de criativos ativos (melhores, piores do dia/semana, acumulado, tempo ativo).
3. O resultado é injetado em `template.html` (mesmo layout/estilo do dashboard anterior) e publicado como `site/index.html` no GitHub Pages.

## Configuração necessária (uma vez só)

- **Settings → Pages → Source**: `GitHub Actions`.
- **Settings → Secrets and variables → Actions → New repository secret**:
  - Nome: `FB_ACCESS_TOKEN`
  - Valor: token do System User do Meta Business Manager, com permissão `ads_read` nas 3 contas de anúncio.

## Rodar localmente (opcional, para testar)

```bash
export FB_ACCESS_TOKEN="seu_token_aqui"
pip install -r requirements.txt
python fetch_and_build.py
# abre site/index.html no navegador
```

## Observação sobre a definição de "leads"

O campo `actions_lead` do Windsor.ai foi reimplementado aqui somando, na resposta da Graph API (campo `actions`), qualquer `action_type` que contenha a palavra `lead` (cobre `lead`, `offsite_conversion.fb_pixel_lead`, `onsite_conversion.lead_grouped`, etc). Vale comparar os números com o dashboard antigo (Windsor) nos primeiros dias para confirmar que bate exatamente — se não bater, ajustar a lista de `action_type` em `is_lead_action()` no `fetch_and_build.py`.

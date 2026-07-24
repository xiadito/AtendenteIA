-- ============================================================
-- Migration 007: Create ai_configs, the per-tenant customizable prompt layer
--
-- Owner of writes: the gym owner, by hand via SQL for the pilot (no UI yet).
-- Owner of reads:  bot/ai_configs.py::get_ai_config(), whose values
--                  bot/ai_context.py::build_system_prompt() injects into the
--                  customizable half of the two-layer system prompt.
--
-- The system prompt has two layers. The protected layer (mission, action-block
-- contract, flow milestones, scheduling rules, safeguards) lives in code and
-- the client never sees or edits it. This table is the customizable layer:
-- gym name, attendant name, tone, business facts, flow emphasis. Its text is
-- treated as UNTRUSTED input and is only interpolated at the fixed points the
-- prompt builder allows, never as a way to rewrite the whole prompt.
--
-- Single-tenant for the pilot (tenant_id 'default'), same convention as
-- owners.tenant_id and trial_bookings.tenant_id. Seeded with obvious bracketed
-- placeholders the owner replaces by hand.
-- ============================================================

CREATE TABLE IF NOT EXISTS ai_configs (
    tenant_id      VARCHAR(64) PRIMARY KEY DEFAULT 'default',
    academy_name   VARCHAR(255) NOT NULL,
    assistant_name VARCHAR(255) NOT NULL,
    tone           TEXT NOT NULL,
    business_info  TEXT NOT NULL,
    flow_emphasis  TEXT NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed the pilot tenant with clear placeholders (replace by hand via SQL).
-- ON CONFLICT DO NOTHING so re-running the seed never overwrites edited values.
INSERT INTO ai_configs (tenant_id, academy_name, assistant_name, tone, business_info, flow_emphasis)
VALUES (
    'default',
    '[NOME DA ACADEMIA]',
    '[NOME DA ATENDENTE]',
    '[TOM/PERSONALIDADE — ex.: simpática, direta, acolhedora, trata o lead pelo nome]',
    '[INFORMAÇÕES DO NEGÓCIO — ex.: modalidades oferecidas (Jiu-Jitsu, CrossFit, musculação), endereço, horários de funcionamento, valores da mensalidade, política da aula experimental gratuita]',
    '[ÊNFASE DO FLUXO — ex.: priorizar agendar a aula experimental o quanto antes; reforçar que a primeira aula é gratuita e sem compromisso]'
)
ON CONFLICT (tenant_id) DO NOTHING;

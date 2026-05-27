Sync Agent
Serviço Python que lê o catálogo de produtos do PDV (Firebird) e replica para o PostgreSQL central usado pelo bot do WhatsApp.
Visão geral

PC da loja                                Railway
┌────────────────────────┐                ┌───────────────────────┐
│ Firebird 2.5 (PDV)     │                │ PostgreSQL central    │
│ DATACAIXA.FDB          │                │ tabela: products      │
└──────────┬─────────────┘                └──────────▲────────────┘
           │ leitura (read-only)                     │ upsert
           │                                         │
┌──────────▼─────────────┐    rede / HTTPS           │
│ sync_agent (este app)  ├───────────────────────────┘
│ - lê produtos          │
│ - faz upsert no PG     │
│ - desativa removidos   │
└────────────────────────┘

        a cada 5 min
        
Estrutura dos arquivos
sync_agent/
├── .env.example         # template de variáveis de ambiente
├── requirements.txt     # dependências Python
├── main.py              # entry point + loop de sync
├── firebird_reader.py   # leitura read-only do Firebird
├── postgres_writer.py   # upsert idempotente no PostgreSQL
└── README.md            # este arquivo

Setup local (dev)
Pré-requisitos:

Python 3.12 instalado
Firebird 2.5 instalado localmente (mesma versão da loja)
PostgreSQL local rodando (banco mercadinho_dev do projeto principal)
Migration 002_create_products.sql já aplicada (Flask aplica no startup)

Passo a passo:
cmd cd sync_agent
py -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
:: editar .env com as credenciais locais
py main.py
Setup em produção (PC da loja)

Copiar a pasta sync_agent/ para o PC da loja (USB ou Git)
Instalar Python 3.12 no PC da loja (se ainda não houver)
Criar venv e instalar dependências:

cmd   cd C:\caminho\sync_agent
   py -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt

Criar .env a partir de .env.example, preenchendo:

Credenciais do Firebird (obtidas com o suporte do PDV Rio)
DATABASE_URL com o usuário restrito sync_agent (ver seção de segurança)


Testar manualmente: py main.py — verificar logs
Configurar inicialização automática (ver seção de operação)

Segurança — usuário PG restrito
O sync_agent não deve usar o usuário admin do PostgreSQL. Crie um usuário dedicado com permissões mínimas, executando uma vez no banco do Railway:
sqlCREATE ROLE sync_agent WITH LOGIN PASSWORD 'gere_uma_senha_forte_aqui';

GRANT CONNECT ON DATABASE railway TO sync_agent;
GRANT USAGE  ON SCHEMA public TO sync_agent;
GRANT SELECT, INSERT, UPDATE ON products TO sync_agent;
GRANT USAGE  ON SEQUENCE products_id_seq TO sync_agent;
Esse usuário só pode tocar na tabela products. Se o .env da loja vazar, o estrago é limitado a essa tabela.
Operação no Windows da loja
Para o agent rodar automaticamente ao ligar o PC, duas opções:
Opção 1 — Task Scheduler (mais simples):

Abrir Agendador de Tarefas
Criar tarefa → Disparador: "Ao fazer logon"
Ação: iniciar programa → cmd /c "cd C:\caminho\sync_agent && venv\Scripts\python main.py"
Marcar "Executar mesmo se o usuário não estiver conectado"

Opção 2 — Windows Service via NSSM (mais robusto):
Instalar NSSM e:
cmdnssm install MercadinhoSyncAgent
:: Application: C:\caminho\sync_agent\venv\Scripts\python.exe
:: Arguments: main.py
:: Startup directory: C:\caminho\sync_agent
nssm start MercadinhoSyncAgent
Vantagem do NSSM: reinício automático em caso de crash; logs gerenciados pelo Windows.
Logs

Arquivo: sync_agent.log (na pasta do app)
Rotação automática: 5 MB por arquivo, mantém 3 históricos
Também imprime no stdout (útil rodando manual)

Solução de problemas
Sintoma | Causa | Provável Ação: Your user name and password are not definedSenha do Firebird error: Conferir FIREBIRD_PASSWORD
 .envAcentos vêm como ??? ou Ã©Charset errado: Trocar FIREBIRD_CHARSET para ISO8859_1unable 
 to complete network requestServiço Firebird paradosc start FirebirdServerDefaultInstanceconnection refused (PostgreSQL)URL do banco errada ou Railway inacessívelConferir DATABASE_URL e ping ao hostpermission denied for table productsUsuário sync_agent sem permissão: Rodar os GRANT da seção de segurançaSync roda mas 0 produtosQuery placeholder não bate com schema realAtualizar PRODUCTS_QUERY em firebird_reader.py
 
Próximos passos

Ajustar PRODUCTS_QUERY em firebird_reader.py com os nomes reais de tabelas e colunas após rodar discover_schema.sql na loja
Conectar o catálogo do PostgreSQL ao ai_context.py do bot (substituir catálogo estático)
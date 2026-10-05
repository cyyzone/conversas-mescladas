# Dashboard de mesclagens do Intercom

Dashboard Streamlit para localizar conversas secundárias mescladas e suas
conversas principais, visualizar os motivos e exportar os resultados para Excel.

## Executar localmente

Instale as dependências e inicie o dashboard:

```powershell
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

Para consultar a API, configure os valores no arquivo local
`.streamlit/secrets.toml`:

```toml
INTERCOM_TOKEN = "seu-token"
INTERCOM_APP_ID = "id-do-workspace"
```

O arquivo de Secrets e o CSV local são ignorados pelo Git. O token não é
solicitado na interface do dashboard. Sem credenciais, o app pode carregar
`conversas_mescladas.csv` local, se o arquivo existir.

## Publicar no Streamlit Community Cloud

1. Publique neste repositório os arquivos do app e `requirements.txt`.
2. Crie um app no Streamlit Community Cloud apontando para `streamlit_app.py`.
3. Em **Settings > Secrets**, adicione:

   ```toml
   INTERCOM_TOKEN = "seu-token"
   INTERCOM_APP_ID = "id-do-workspace"
   ```

O token e o ID do workspace são lidos dos Secrets do Streamlit; não são
exibidos nem solicitados na página.

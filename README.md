## Sui

Minimal local RPA studio with SQLite persistence and Rustwright recipe execution.

### Run locally

Start the backend from the repository root:

```powershell
python main.py
```

In a second terminal, start the frontend:

```powershell
cd frontend
npm install
npm run dev
```

Open the Vite URL shown in the terminal. The API runs at `http://127.0.0.1:8000`.

For a single backend-served build, run `npm run build` in `frontend`, then open `http://127.0.0.1:8000`.

Local SQLite data is stored in `data/sui.db`; generated recipe versions are stored under `data/generated/`.

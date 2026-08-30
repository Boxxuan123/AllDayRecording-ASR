# AllDayRecording Web Workbench

Vue 3 + TypeScript + Vite frontend for the local AllDayRecording-ASR workbench.

```powershell
npm install
npm test
npm run dev
npm run build
```

Development requests use the existing same-origin `/api/*` contract. `npm run dev` proxies these
requests to a local Python backend on port `8765`. The production build writes stable assets directly
to `../src/allday_asr/web_assets`; commit those assets so the Python package remains runnable without
Node.js.

Do not edit files under `src/allday_asr/web_assets` by hand. Their source lives in this directory.

# KHALINOS Engine Bake-off

This experiment compares Unity and Godot as deterministic production adapters. It is
not connected to the KHALINOS product path and does not use a model or a paid cloud
service.

Both engines receive the same five-region topology plan. A trusted generator must:

1. materialize every region as an engine-native scene;
2. import or compile the generated project without manual editor work;
3. traverse the declared route in a real runtime;
4. capture one PNG for every visited region; and
5. produce a Windows desktop build.

Run from the repository root:

```powershell
.venv\Scripts\python experiments\engine_bakeoff\run_bakeoff.py `
  --godot C:\path\to\Godot.exe `
  --unity "C:\Program Files\Unity\Hub\Editor\6000.3.11f1\Editor\Unity.exe" `
  --workspace C:\tmp\khalinos-engine-bakeoff\run
```

Generated projects and engine binaries remain outside the repository. The committed
report contains only measurements, failure receipts, and the exact input digest.

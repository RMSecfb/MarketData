# scenarios（歷史統計引擎，Phase 0）

跨資產「歷史情境統計」研究引擎。僅做歷史條件 vs 無條件比較，非預測、非警示。
- `engine.py`：條件／結果／壓力時段／stationary bootstrap／右設限／四態摘要
- `test_engine.py`：合成資料單元測試（`pytest scenarios/test_engine.py`）
- `phase0_inventory.py`：資料盤點（只輸出摘要，不提交第三方原始序列）

不改動既有 latest.json 欄位、不進入 checks_for()／data_quality。

"""Price-refresh agent: keeps the model_prices table current under layered guards.

Phase 0 ships the pure validation layers (validate.py) and the storage seam +
proposals CRUD (store.py). validate.py imports no DB code and stays side-effect free.
"""

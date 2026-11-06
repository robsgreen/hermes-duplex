"""Voice provider adapters and the interface they share.

``base`` is the interface, ``wire`` the shared WebSocket client, and ``registry`` the ``ProviderSpec``
for each ``provider.name``. Each provider has its own package: ``gpt_live``, ``realtime`` (the shared
adapter, with the ``openai`` and ``xai`` dialects), and ``fake``.
"""

"""The prompt pieces every provider shares.

``base`` holds the ``VoicePrompter`` interface and its inputs; ``blocks`` the rules every prompt
states; and ``notices`` the short spoken notices, worded once for every provider. Each provider's own
prompter lives in its package under ``providers/`` (``prompt.py``), following that vendor's prompting
guide.
"""

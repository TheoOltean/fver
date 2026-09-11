"""The LLM agent: prompt assembly, the propose -> check -> repair loop, and
everything that talks to the Anthropic API.

Only `client.py` imports the anthropic SDK. Only `loop.py` drives a Backend.
"""

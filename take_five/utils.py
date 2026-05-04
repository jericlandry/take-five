from langsmith import Client

from take_five.summaries import SUMMARY_PROMPT_NAME

ls_client = Client()

def fetch_prompt(prompt_name):
    """
    Pull the named prompt from LangSmith Hub and return a runnable chain
    ready to invoke with Claude Haiku.
    """
    return ls_client.pull_prompt(prompt_name)
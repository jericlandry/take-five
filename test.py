from take_five.repository import TakeFiveRepository
from take_five.summaries import format_conversation, fetch_prompt, generate_weekly_digest
import sys
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file

def test_repository(repo=None):
    if repo is None:
        print("No repository provided for testing.")
        return
    
    circle_ext_id = "groupme_1234"
    person_ext_id = "groupme_456" 

    circle = repo.upsert_circle(circle_ext_id, "Yet Another Test Circle")
    person = repo.upsert_person(person_ext_id, "Test Person", "family")
    membership = repo.add_to_circle(circle_ext_id, person_ext_id, "member")
    message = repo.log_message(circle_ext_id, person_ext_id, "This is a test message.")
    print(message)

def main():
    
    circle_ext_id = "114182896"
    
    digest = generate_weekly_digest(circle_ext_id)

    print("Generated Digest:")
    print(digest)

    return 0

if __name__ == "__main__":
    # This block only runs if the script is executed directly
    sys.exit(main())

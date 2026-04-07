from take_five.repository import TakeFiveRepository
import sys



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
    repo = TakeFiveRepository({
        'dbname': 'takefive',
        'user': 'jeric',
        'password': 'M7CzRtB67FcmZj6kwBv04zYy5eDwv7xN',
        'host': 'dpg-d78po2h5pdvs73b7l7rg-a.virginia-postgres.render.com',
        'port': 5432
    }) 

    test_repository(repo)

    return 0

if __name__ == "__main__":
    # This block only runs if the script is executed directly
    sys.exit(main())

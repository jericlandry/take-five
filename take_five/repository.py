import psycopg2
import json
from psycopg2.extras import RealDictCursor, Json
from typing import List, Dict, Any, Optional

class TakeFiveRepository:
    def __init__(self, db_config: Dict[str, str]):
        self.db_config = db_config

    def _execute(self, query: str, params: tuple = (), fetch: str = 'one'):
        with psycopg2.connect(**self.db_config, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                conn.commit()
                if fetch == 'one': return cur.fetchone()
                if fetch == 'all': return cur.fetchall()
                return None

    # --- PEOPLE ---
    def upsert_person(self, external_id: str, name: str, p_type: str, **kwargs) -> Dict:
        """Creates or updates a person. p_type: senior, family, aide, nurse, agent"""
        query = """
            INSERT INTO people (external_id, external_type, name, type, email, phone, timezone)
            VALUES (%(ext_id)s, 'groupme', %(name)s, %(type)s, %(email)s, %(phone)s, %(tz)s)
            ON CONFLICT (external_id) 
            DO UPDATE SET name = EXCLUDED.name, type = EXCLUDED.type
            RETURNING *;
        """
        params = {
            'ext_id': str(external_id), 'name': name, 'type': p_type,
            'email': kwargs.get('email'), 'phone': kwargs.get('phone'),
            'tz': kwargs.get('timezone', 'America/Chicago')
        }
        return self._execute(query, params)

    def get_person_by_external_id(self, external_id: str) -> Optional[Dict]:
        return self._execute("SELECT * FROM people WHERE external_id = %s;", (str(external_id),))

    # --- CARE CIRCLES ---
    def upsert_circle(self, external_id: str, name: str) -> Dict:
        """Creates or updates a circle based on GroupMe Group ID."""
        query = """
            INSERT INTO care_circles (external_id, external_type, name)
            VALUES (%s, 'groupme', %s)
            ON CONFLICT (external_id) 
            DO UPDATE SET name = EXCLUDED.name
            RETURNING *;
        """
        return self._execute(query, (str(external_id), name))

    def get_circle_by_external_id(self, external_id: str) -> Optional[Dict]:
        return self._execute("SELECT * FROM care_circles WHERE external_id = %s;", (str(external_id),))

    # --- MEMBERSHIPS ---
    def add_to_circle(self, circle_ext_id: str, person_ext_id: str, role: str) -> Dict:
        """Links person to circle using external IDs."""
        query = """
            INSERT INTO circle_memberships (circle_id, person_id, role)
            VALUES (
                (SELECT id FROM care_circles WHERE external_id = %s),
                (SELECT id FROM people WHERE external_id = %s),
                %s
            )
            ON CONFLICT (circle_id, person_id) DO UPDATE SET role = EXCLUDED.role
            RETURNING *;
        """
        return self._execute(query, (str(circle_ext_id), str(person_ext_id), role))

    # --- MESSAGES ---
    def log_message(self, circle_ext_id: str, person_ext_id: Optional[str], 
                    body: str, msg_type: str = 'inbound', 
                    direction: str = 'inbound', raw_data: Optional[Dict] = None) -> Dict:
        """
        Logs a message. person_ext_id can be None for system/agent notes.
        parsed_data should be a Python dict (mood_score, meds_taken, etc.)
        """
        query = """
            INSERT INTO messages (circle_id, person_id, message_type, direction, body, raw)
            VALUES (
                (SELECT id FROM care_circles WHERE external_id = %s),
                (SELECT id FROM people WHERE external_id = %s),
                %s, %s, %s, %s
            ) RETURNING *;
        """
        # Json() wrapper from psycopg2.extras handles the dict-to-jsonb conversion
        return self._execute(query, (
            str(circle_ext_id), 
            str(person_ext_id) if person_ext_id else None, 
            msg_type, direction, body, 
            Json(raw_data) if raw_data else None
        ))

    def get_recent_messages(self, circle_ext_id: str, limit: int = 50) -> List[Dict]:
        query = """
            SELECT m.*, p.name as author_name 
            FROM messages m
            LEFT JOIN people p ON m.person_id = p.id
            WHERE m.circle_id = (SELECT id FROM care_circles WHERE external_id = %s)
            ORDER BY m.sent_at DESC LIMIT %s;
        """
        return self._execute(query, (str(circle_ext_id), limit), fetch='all')


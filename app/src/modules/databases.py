import logging
import os
import traceback
from fastapi import HTTPException
from langchain_postgres import PGVector
import psycopg
import yaml
from app.src import constants
from langchain_openai import OpenAIEmbeddings
from app.src.modules.auth import Authentication

logger = logging.getLogger("databases")

def get_connection_string():
    # conn_string = f"host='{db_host}' port='{db_port}' dbname='{
    #     db_name}' user='{db_user}' password='{db_password}' sslmode='require'"

    conn_string = os.getenv("DATABASE_URL")
    print('00000000000000000000000000000000000000000000000000000000000')
    print(conn_string)
    return conn_string


def get_alchemy_conn_string():

    # For reference:
    # conn_string = f"postgresql+psycopg://{db_user}:{
    #         db_password}@{db_host}:{db_port}/{db_name}"

    conn_string = os.getenv("VECTORSTORE_URL")

    return conn_string


class PGVectorManager:
    def __new__(cls):
        if not hasattr(cls, 'instance'):
            cls.instance = super(PGVectorManager, cls).__new__(cls)
        return cls.instance

    def __init__(self):
        self.logger = logging.getLogger("PGVectorManager")
        connection_string = get_alchemy_conn_string()
        self.connection_string = connection_string.replace(
            "psycopg2", "psycopg")
        # Mask sensitive parts of connection string for logging
        masked_conn = self.connection_string
        if "://" in masked_conn:
            parts = masked_conn.split("://")
            if len(parts) > 1:
                auth_parts = parts[1].split("@")
                if len(auth_parts) > 1:
                    masked_conn = f"{parts[0]}://****:****@{auth_parts[1]}"
        self.logger.debug(f"Connection string: {masked_conn}")

    def return_vector_store(self, collection_name, async_mode) -> PGVector:
        try:
            logger.info(f"1")
            self.vectorstore = PGVector(
                embeddings=OpenAIEmbeddings(model=constants.EMBEDDINGS_MODEL),
                collection_name=collection_name,
                connection=self.connection_string,
                use_jsonb=True,
                async_mode=async_mode
            )
            logger.info(f"2")
            return self.vectorstore
        except Exception as e:
            self.logger.error(f"Error creating vector store: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Database connection error: {str(e)}")

    async def insert_documents(self, collection_name, documents, async_mode=True):
        try:
            self.logger.info(f"Starting to insert {len(documents)} documents into collection {collection_name}")
            vectorstore = self.return_vector_store(collection_name, async_mode)
            self.logger.info("Vector store created successfully")
            await vectorstore.aadd_documents(documents)
            self.logger.info("Documents added successfully")
        except Exception as e:
            self.logger.error(f"Error inserting documents: {str(e)}")
            self.logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=f"Error inserting documents: {str(e)}")

    def get_retriever(self, collection_name, async_mode, search_kwargs=None):
        try:
            vectorstore = self.return_vector_store(collection_name, async_mode)
            default_kwargs = {'k': 5}
            if search_kwargs:
                default_kwargs.update(search_kwargs)
            retriever = vectorstore.as_retriever(search_kwargs=default_kwargs)
            return retriever
        except Exception as e:
            self.logger.error(f"Error in get_retriever: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Error getting retriever: {str(e)}")

    def close(self):
        if self.vectorstore is not None:
            if hasattr(self.vectorstore, "__async_engine"):
                self.vectorstore.__async_engine.close()
            if hasattr(self.vectorstore, "__engine"):
                self.vectorstore.__engine.close()

    def check_collection_exists(self, collection_name):
        """Check if a collection exists and has data"""
        try:
            vectorstore = self.return_vector_store(collection_name, False)
            # Try to get one document from the collection
            retriever = vectorstore.as_retriever(search_kwargs={'k': 1})
            docs = retriever.invoke("test")
            return len(docs) > 0
        except Exception as e:
            self.logger.error(f"Error checking collection {collection_name}: {str(e)}")
            return False

    def get_collection_stats(self, collection_name):
        """Get statistics about a collection"""
        try:
            vectorstore = self.return_vector_store(collection_name, False)
            # Get all documents from the collection
            retriever = vectorstore.as_retriever(search_kwargs={'k': 1000})  # Get a large number to count all
            docs = retriever.invoke("test")
            return {
                "exists": True,
                "document_count": len(docs),
                "sample_documents": [doc.page_content[:200] for doc in docs[:3]]  # First 200 chars of first 3 docs
            }
        except Exception as e:
            self.logger.error(f"Error getting stats for collection {collection_name}: {str(e)}")
            return {
                "exists": False,
                "error": str(e)
            }


class ConversationDB:
    def __new__(cls):
        if not hasattr(cls, 'instance'):
            cls.instance = super(ConversationDB, cls).__new__(cls)
        return cls.instance

    def __init__(self):
        self.logger = logging.getLogger("CoversationDB")
        try:
            self.logger.info(
                "Creating connection string for conversation db")

            conn_string = get_connection_string()
            self.logger.info(f"Connection string: {conn_string}")

            if conn_string is None:
                raise AttributeError(
                    'No connection string provided for conversation db')
            self.conn_string = conn_string
            print(conn_string)
            print("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
            conn = psycopg.connect(conn_string)
            cursor = conn.cursor()
            # uncomment if you want to create the table again
            # cursor.execute('DROP TABLE IF EXISTS queries')
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS queries (
            id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
            Convo_ID TEXT,
            Question TEXT,
            Answer TEXT,
            Prompt TEXT,
            timestamp timestamp default current_timestamp,
            response_time double precision, 
            rating integer, 
            review text,
            user_id TEXT
            )
                ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS Users (
            id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
            name TEXT,
            email TEXT,
            designation TEXT,
            department TEXT,
            role TEXT,
            firebase_uid TEXT,
            created_at timestamp default current_timestamp, 
            updated_at timestamp default current_timestamp,
            last_login timestamp,
            last_session_duration double precision
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS AllowedEmails (
            id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
            name TEXT,
            email TEXT,
            role TEXT,
            allowed_by TEXT not null,
            created_at timestamp default current_timestamp, 
            updated_at timestamp default current_timestamp
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS AllowedDomains (
            id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
            domain_name Text,
            allowed_by TEXT not null,
            created_at timestamp default current_timestamp, 
            updated_at timestamp default current_timestamp
            )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conversation (
                    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
                    user_id text not null,
                    first_question Text,
                    description TEXT,
                    created_at timestamp default current_timestamp                           
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS docProcTemplates (
                    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
                    user_id text not null,
                    template_name Text,
                    attributes TEXT,
                    created_at timestamp default current_timestamp                           
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS allowedips (
                    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
                    ip_address TEXT,
                    created_at timestamp default current_timestamp,
                    updated_at timestamp default current_timestamp
                           )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS parentdocuments (
                    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
                    parent_document TEXT,
                    created_at timestamp default current_timestamp
                )
                           ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS prompts (
                    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
                    llm_model TEXT,
                    persona TEXT,
                    glossary TEXT,
                    tone TEXT,
                    response_length TEXT,
                    content TEXT,
                    created_at timestamp DEFAULT current_timestamp,
                    updated_at timestamp DEFAULT current_timestamp
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS files (
                    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
                    file_name text NOT NULL,
                    url text NOT NULL,
                    user_id text NOT NULL,
                    created_at timestamp DEFAULT current_timestamp,
                    updated_at timestamp DEFAULT current_timestamp,
                    active boolean DEFAULT true
                );
                ''')

            conn.commit()
            cursor.close()
            conn.close()
        except psycopg.Error as err:
            self.logger.exception(err)

    async def add_files(self, data, user_id):
        try:
            # Prepare data for batch insertion
            self.logger.info(f"Received data in add_files: {data}")
            self.logger.info(f"User ID in add_files: {user_id}")
            values = [(item['filename'], item['url'], user_id)
                      for item in data]
            self.logger.info(f"Prepared values for insertion: {values}")

            conn = psycopg.connect(self.conn_string)
            cursor = conn.cursor()
            cursor.executemany('''
                INSERT INTO files (file_name, url, user_id)
                VALUES (%s, %s, %s)
            ''', values)
            conn.commit()
            cursor.close()
            conn.close()
            self.logger.info("Successfully inserted files into database")

        except psycopg.Error as err:
            self.logger.exception(err)

    async def get_files(self):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT file_name, url, user_id, created_at, updated_at, active
            FROM public.files 
            order by created_at desc
        ''',)

        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def get_active_files(self):

        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT q.file_name
            FROM public.files q 
            where q.active = true
            ''')
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def delete_file(self, file_name):
        try:
            self.conn = psycopg.connect(self.conn_string)
            cursor = self.conn.cursor()
            cursor.execute('''
                Delete
                FROM public.files 
                where file_name=%s;
                ''', (file_name,))
            # rows = cursor.fetchall(
            self.conn.commit()
            cursor.close()
            self.conn.close()
            return "Deleted"
        except psycopg.Error as err:
            self.logger.exception(err)

    async def toggle_file_active(self, file_name, active_flag):
        try:
            self.conn = psycopg.connect(self.conn_string)
            cursor = self.conn.cursor()
            cursor.execute('''
                UPDATE public.files
                SET active = %s
                WHERE file_name = %s;
            ''', (active_flag, file_name))
            # rows = cursor.fetchall(
            self.conn.commit()
            cursor.close()
            self.conn.close()
            return "Updated"
        except psycopg.Error as err:
            self.logger.exception(err)

    async def delete_file_embeddings(self, file_name):
        try:
            self.conn = psycopg.connect(self.conn_string)
            cursor = self.conn.cursor()
            cursor.execute('''
                DELETE FROM public.langchain_pg_embedding
                WHERE cmetadata ->> 'source' LIKE %s;
            ''', ('%' + file_name + '%',))

            self.conn.commit()
            cursor.close()
            self.conn.close()
            return "Deleted Embeddings"
        except psycopg.Error as err:
            self.logger.exception(err)

    async def insert_query(self, conversation_id, query, response, prompt, response_time, user_id):
        try:
            self.logger.info(f"Inserting query: {query} into conversation: {conversation_id}")
            conn = psycopg.connect(self.conn_string)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO queries (convo_ID, Question, Answer, Prompt, response_time, user_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id;
            ''', (conversation_id, query, response, prompt, response_time, user_id))
            id = cursor.fetchall()
            query_id = id[0][0]
            conn.commit()
            cursor.close()
            conn.close()

            return query_id
        except psycopg.Error as err:
            self.logger.exception(err)

    def insert_hr_query(self, conversation_id, query, response, context, response_time, user_id):
        self.logger.info(
            f"Inserting query: {query} into conversation: {conversation_id}"
        )
        conn = psycopg.connect(self.conn_string)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO hr_questions (convo_ID, Question, Answer, context, response_time, user_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id;
        ''', (conversation_id, query, response, context, response_time, user_id))
        id = cursor.fetchall()
        query_id = id[0][0]
        conn.commit()
        cursor.close()
        conn.close()

        return query_id

    async def insert_prompt(self, prompt):
        self.logger.info(f"Inserting query: {prompt} into prompts:")
        conn = psycopg.connect(self.conn_string)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO prompts (llm_model, persona, glossary, tone, response_length ,content)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id;
        ''', (prompt.llm_model, prompt.persona, prompt.glossary, prompt.tone, prompt.response_length, prompt.content,))
        id = cursor.fetchall()
        query_id = id[0][0]
        conn.commit()
        cursor.close()
        conn.close()

        return query_id

    async def get_prompt(self):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT llm_model, persona, glossary, tone, response_length ,content FROM prompts
            ORDER BY created_at DESC
            LIMIT 1;
        ''')
        row = cursor.fetchone()
        cursor.close()
        self.conn.close()
        return row

    def get_rows(self, num_months, number_of_rows=10):
        try:
            conn = psycopg.connect(self.conn_string)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT *
                FROM queries
                WHERE timestamp >= CURRENT_DATE - INTERVAL '%s month'
                AND timestamp < CURRENT_DATE
                ORDER BY timestamp DESC
                LIMIT %s;
            ''', (num_months, number_of_rows))
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            return rows
        except psycopg.Error as err:
            self.logger.exception(err)
            return None

    def get_daily_usage_rows(self):
        try:
            conn = psycopg.connect(self.conn_string)
            cursor = conn.cursor()
            query = """
             SELECT timestamp
             FROM queries
             WHERE timestamp >= CURRENT_DATE - INTERVAL '1 month'
             AND timestamp < CURRENT_DATE;
             """
            cursor.execute(query)
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            return rows
        except psycopg.Error as err:
            self.logger.exception(err)
            return None

    async def insert_review_and_rating(self, query_id, rating, review):
        try:
            conn = psycopg.connect(self.conn_string)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE queries
                SET rating = %s,
                    review = %s
                WHERE id = %s;
            ''', (rating, review, query_id))
            conn.commit()
            cursor.close()
            conn.close()
        except psycopg.Error as err:
            self.logger.exception(err)

    async def insert_hr_review_and_rating(self, query_id, rating, review):
        try:
            conn = psycopg.connect(self.conn_string)
            cursor = conn.cursor()
            query = """
                UPDATE hr_questions SET
                """

            if rating is not None:
                query += "rating = %s ,"
                inputs = (rating, query_id)

            if review is not None:
                query += " review = %s"
                inputs = (review, query_id)

            if review is not None and rating is not None:
                inputs = (rating, review, query_id)

            if query.endswith(","):
                query = query[:-1]

            query += " WHERE id = %s;"

            cursor.execute(query, inputs)

            conn.commit()
            cursor.close()
            conn.close()
        except psycopg.Error as err:
            self.logger.exception(err)

    async def insert_google_user(self, name, email, uid, role):
        conn = psycopg.connect(self.conn_string)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO Users (name, email, firebase_uid, role)
            VALUES (%s, %s, %s, %s)
            RETURNING id;
        ''', (name, email, uid, role))
        localid = cursor.fetchall()
        user_id = localid[0][0]
        conn.commit()
        cursor.close()
        conn.close()
        return user_id

    async def insert_conversation(self, user_id, first_question):
        conn = psycopg.connect(self.conn_string)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO conversation (user_id, first_question)
            VALUES (%s, %s)
            RETURNING id;
        ''', (user_id, first_question))
        conversation_id = cursor.fetchall()
        conversation_id = conversation_id[0][0]
        conn.commit()
        cursor.close()
        conn.close()
        return conversation_id

    async def update_user(self, id, column, value):
        conn = psycopg.connect(self.conn_string)
        cursor = conn.cursor()
        cursor.execute(f'''
            UPDATE Users
            SET {column} = %s
            WHERE id = %s
        ''', (value, id))
        conn.commit()
        cursor.close()
        conn.close()

    async def does_user_exist(self, email):
        conn = psycopg.connect(self.conn_string)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT *
            FROM Users
            WHERE email = %s
        ''', (email,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        if len(rows) > 0:
            return True, rows[0]
        return False, None

    async def change_user_role(self, role, email):
        try:
            conn = psycopg.connect(self.conn_string)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE Users
                SET role = %s, updated_at = current_timestamp
                WHERE email = %s
            ''', (role, email))

            cursor.execute(
                """
                UPDATE allowedemails
                SET role = %s, updated_at = current_timestamp
                WHERE email = %s
                """,
                (role, email)
            )
            conn.commit()
            cursor.close()
            conn.close()
            return True
        except psycopg.Error as err:
            self.logger.exception(err)
            return False

    async def insert_user(self, name, email, password, designation=None, department=None, role=constants.DEFAULT_ROLE):
        try:
            self.conn = psycopg.connect(self.conn_string)
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO Users (name, email, designation, department, role)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
            ''', (name, email, designation, department, role))
            user_id = cursor.fetchall()
            user_id = str(user_id[0][0])
            self.auth = Authentication()
            user = None
            if user_id is not None:
                user = await self.auth.signup(id=user_id, email=email, name=name, password=password, role=role)

            if user is None:
                raise RuntimeError("Firebase User not created")

            loggedin_user = await self.auth.sign_in_with_email_and_password(email=user.email, password=password)
            print(loggedin_user)

            # res = await self.auth.send_email_verification(loggedin_user["idToken"])

            res = await self.auth.update_user({"uid": user_id, "emailVerified": True})

            self.logger.info(res)

            # await self.auth.sign_out_user(user_id)
            self.conn.commit()
            cursor.close()
            self.conn.close()
            return user
        except Exception:
            self.logger.info(
                "There has been an error in creating a user, rolling back and reversing all changes")
            self.logger.exception(traceback.format_exc())
            await self.auth.delete_user(user_id)
            self.conn.rollback()
            self.conn.close()
            raise HTTPException(
                status_code=500, detail="Failed to create user")

    async def allowed_email_addresses(self):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT *
            FROM allowedemails
        ''')
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def select_all_from_allowed_email_addresses(self):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT *
            FROM allowedemails
        ''')
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def allowed_email_domains(self):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT domain_name
            FROM alloweddomains
        ''')
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def get_conversation(self, convo_id):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT *
            FROM queries
            WHERE convo_id = %s
        ''', (convo_id,))
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def get_hr_conversation(self, convo_id):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT *
            FROM hr_questions
            WHERE convo_id = %s
        ''', (convo_id,))
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def get_conversation_ids(self, user_id):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT id, first_question
            FROM conversation
            WHERE user_id = %s
        ''', (user_id,))
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def get_user_by_email(self, email):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT *
            FROM Users
            WHERE email = %s
        ''', (email,))
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def get_ask_engr_queries(self):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT u.name, u.email, q.question, q.answer, q.review, q.rating, q.timestamp, q.response_time
            FROM public.queries q join users u on q.user_id=u.id::text
            ORDER BY timestamp desc
            LIMIT 100;
        ''')
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def get_ask_hr_queries(self):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT u.name, u.email, q.question, q.answer, q.review, q.rating, q.created_at, q.response_time
            FROM public.hr_questions q join users u on q.user_id=u.id::text
            ORDER BY created_at desc
            LIMIT 100;
        ''')
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def get_ask_engr_response_time(self):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT response_time
            FROM public.queries
            ORDER BY timestamp desc
            LIMIT 100;
        ''')
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        float_values = [tup[0] for tup in rows]
        average = sum(float_values) / len(float_values)
        res = {}
        res['values'] = float_values
        res['avg'] = average
        return res

    async def get_ask_hr_response_time(self):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT response_time
            FROM public.hr_questions
            ORDER BY created_at desc
            LIMIT 100;
        ''')
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        float_values = [tup[0] for tup in rows]
        average = sum(float_values) / len(float_values)
        res = {}
        res['values'] = float_values
        res['avg'] = average
        return res

    async def get_ask_engr_daily_usage(self):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT DATE_TRUNC('day', timestamp) AS usage_date, COUNT(*) AS total_usage
            FROM public.queries
            WHERE timestamp >= CURRENT_DATE - INTERVAL '1 month' AND timestamp < CURRENT_DATE + INTERVAL '1 day'
            GROUP BY usage_date
            ORDER BY usage_date;
        ''')
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def get_ask_hr_daily_usage(self):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT DATE_TRUNC('day', created_at) AS usage_date, COUNT(*) AS total_usage
            FROM public.hr_questions
            WHERE created_at >= CURRENT_DATE - INTERVAL '1 month' AND created_at < CURRENT_DATE + INTERVAL '1 day'
            GROUP BY usage_date
            ORDER BY usage_date ASC;
        ''')
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def save_template(self, user_id, template_name, attributes):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO docproctemplates (user_id, template_name, attributes)
            VALUES (%s, %s, %s)
            RETURNING id;
        ''', (user_id, template_name, attributes))
        self.conn.commit()
        cursor.close()
        self.conn.close()
        return True

    async def get_user_templates(self, user_id):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT template_name, attributes
            FROM docproctemplates
            WHERE user_id = %s
        ''', (user_id,))
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def get_users(self):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT name, email, role, created_at, last_login, last_session_duration
            FROM users
            ORDER BY last_login desc
            NULLS last
        ''')
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def update_user(self, email, column, value):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute(f'''
            UPDATE users
            SET {column} = %s
            WHERE email = %s
        ''', (value, email))
        self.conn.commit()
        cursor.close()
        self.conn.close()
        return True

    async def get_allowed_ips(self):
        self.conn = psycopg.connect(self.conn_string)
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT ip_address
            FROM allowedips
        ''')
        rows = cursor.fetchall()
        cursor.close()
        self.conn.close()
        return rows

    async def get_file_names_by_collection(self, collection_name: str):
        """Get all file names associated with a specific collection from the langchain_pg_embedding table."""
        try:
            self.conn = psycopg.connect(self.conn_string)
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT DISTINCT cmetadata ->> 'source' as file_name
                FROM public.langchain_pg_embedding
                WHERE cmetadata ->> 'collection_name' = %s
            ''', (collection_name,))
            rows = cursor.fetchall()
            cursor.close()
            self.conn.close()
            # Extract file names from the results and remove any None values
            file_names = [row[0] for row in rows if row[0] is not None]
            return file_names
        except psycopg.Error as err:
            self.logger.exception(err)
            raise err

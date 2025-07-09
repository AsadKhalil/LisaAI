from app.src.modules.databases import PGVectorManager
import asyncio

async def check_index():
    manager = PGVectorManager()
    vectorstore = manager.return_vector_store('lisa', False)
    
    # Search for panadol
    docs = vectorstore.similarity_search('panadol', k=5)
    print('Found documents:', len(docs))
    
    # Print details of each document
    for i, doc in enumerate(docs, 1):
        print(f'\nDocument {i}:')
        print('Source:', doc.metadata.get('source'))
        print('Content preview:', doc.page_content[:200])
    
    manager.close()

if __name__ == "__main__":
    asyncio.run(check_index()) 
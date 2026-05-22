import os
import sqlite3
from services import DbService

def fix_database_consistency():
    project_root = os.path.dirname(os.path.abspath(__file__))
    db_path = DbService.default_db_path()
    if not os.path.exists(db_path):
        print(f"❌ 未找到数据库文件: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    print("🔍 开始进行 [数据库 ↔ 硬盘] 一致性自检...\n")

    # 1. 查找所有状态为 downloaded 的文档及其关联的文件路径
    cursor.execute(
        """
        SELECT d.document_id, d.native_id, d.title, f.path, f.kind
        FROM documents d
        LEFT JOIN files f ON d.document_id = f.document_id
        WHERE d.status IN ('downloaded', 'completed')
        """
    )
    
    records = cursor.fetchall()
    inconsistent_docs = set()
    
    for row in records:
        doc_id, native_id, title, file_path, file_type = row
        
        # 2. 如果查不到文件路径，或者文件在硬盘上不存在
        if not file_path:
            inconsistent_docs.add((doc_id, native_id, title))
            continue

        # files.path 可能是相对路径（相对于项目根目录）或绝对路径
        resolved_path = file_path if os.path.isabs(file_path) else os.path.join(project_root, file_path)
        if not os.path.exists(resolved_path):
            inconsistent_docs.add((doc_id, native_id, title))

    # 3. 执行修复操作
    if not inconsistent_docs:
        print("✅ 完美！数据库中所有标记为已下载的史料，在硬盘上均完好无损。")
    else:
        print(f"⚠️ 发现 {len(inconsistent_docs)} 份史料存在“数据不一致”（数据库显示已下载，但硬盘文件丢失）！\n")
        
        for doc_id, native_id, title in inconsistent_docs:
            print(f"   -> 修复: {title} (Ref: {native_id})")
            
            # 将主表状态退回为 'discovered'，让爬虫下次遇到时重新下载
            cursor.execute("UPDATE documents SET status = 'discovered' WHERE document_id = ?", (doc_id,))
            # 删除失效的物理路径记录
            cursor.execute("DELETE FROM files WHERE document_id = ?", (doc_id,))
        
        conn.commit()
        print(f"\n🛠️ 修复完成！已将这 {len(inconsistent_docs)} 份史料的状态重置。下次运行爬虫将自动重新下载它们。")

    conn.close()

if __name__ == "__main__":
    fix_database_consistency()
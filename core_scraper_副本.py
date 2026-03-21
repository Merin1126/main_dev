# core_scraper.py
import time
import sys
import os
import traceback
import re    
import json
import zipfile  
import io       
import threading         
import queue    
import requests 

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ==========================================
# 🐝 全新后台打工人：纯 API 文件拉取引擎
# ==========================================
def api_download_worker(task_queue, stop_event):
    """
    后台隐形打工人：死盯队列，用纯代码暴力拉取 PDF
    """
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36'
    }

    while not stop_event.is_set():
        try:
            task = task_queue.get(timeout=2)
        except queue.Empty:
            continue
        
        if task is None:
            task_queue.task_done()
            break

        viewer_url = task['url']
        save_path = task['save_path']
        doc_title = task['title']
        vip_cookies = task.get('vip_cookies', [])
        user_agent = task.get('user_agent', '')

        try:
            print(f"  -> 🐝 [打工人接单] 正在后台破解并下载: {doc_title}")
            
            for cookie in vip_cookies:
                session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain'))
            headers['User-Agent'] = user_agent
            
            res = session.get(viewer_url, headers=headers, timeout=15)
            if "Just a moment" in res.text or "Cloudflare" in res.text:
                print(f"  -> 🛑 [打工人报错] 糟糕！通行证失效，被 Cloudflare 拦截: {doc_title}")
                continue 
                
            html = res.text
            
            mid_match = re.search(r'mid=["\'](M\d+)["\']', html)
            if not mid_match:
                print(f"  -> ❌ [打工人报错] 找不到隐藏的 mid，任务失败。")
                continue
            mid_val = mid_match.group(1)
            
            id_key = re.search(r'name=["\'](id_\d+)["\']', html).group(1)
            id_val = re.search(r'ID=(M\d+)', viewer_url).group(1)

            if "digital.archives.go.jp" in viewer_url:
                base_acv = "https://www.digital.archives.go.jp/acv/auto_conversion"
            else:
                base_acv = "https://www.jacar.archives.go.jp/acv/auto_conversion"
                
            sizeget_url = f"{base_acv}/sizeget?mid={mid_val}&dltype=pdf"
            
            print(f"  -> 📡 [打工人] 开始轮询打包进度...")
            poll_count = 0
            while not stop_event.is_set() and poll_count < 40:
                poll_count += 1
                size_res = session.get(sizeget_url, headers=headers, timeout=10)
                size_str = size_res.text.strip()
                
                if poll_count % 3 == 0: 
                    print(f"  -> 💬 [服务器回应] 状态码: {size_res.status_code} | 长度: {len(size_str)}")
                
                is_ready = False
                try:
                    resp_data = json.loads(size_str)
                    if "imageContents" in resp_data and "fileSize" in resp_data["imageContents"]:
                        file_size = int(resp_data["imageContents"]["fileSize"])
                        if file_size > 0:
                            is_ready = True
                            print(f"  -> ✅ [打工人] 服务器打包完成！文件大小: {file_size / 1024 / 1024:.2f} MB")
                except Exception:
                    if size_str.isdigit() and int(size_str) > 0:
                        is_ready = True
                        print(f"  -> ✅ [打工人] 服务器打包完成！文件大小: {int(size_str) / 1024 / 1024:.2f} MB")
                
                if is_ready:
                    break
                    
                time.sleep(1.5)

            if poll_count >= 40:
                print(f"  -> 🛑 [打工人报错] 轮询超时！服务器一直没有返回有效大小。")
                continue 

            if stop_event.is_set():
                break

            dl_url = f"{base_acv}/download"
            payload = {"DL_TYPE": "pdf", id_key: id_val}
            
            req_headers = headers.copy()
            req_headers['Referer'] = viewer_url
            if "digital.archives.go.jp" in viewer_url:
                req_headers['Origin'] = "https://www.digital.archives.go.jp"
            else:
                req_headers['Origin'] = "https://www.jacar.archives.go.jp"

            dl_res = session.post(dl_url, data=payload, headers=req_headers, timeout=60)
            
            if dl_res.status_code == 200:
                raw_data = dl_res.content
                
                if raw_data.startswith(b'PK\x03\x04'):
                    print("  -> 📦 检测到标准 ZIP 压缩包，正在解压提取 PDF...")
                    try:
                        with zipfile.ZipFile(io.BytesIO(raw_data)) as z:
                            file_names = z.namelist()
                            if file_names:
                                pdf_content = z.read(file_names[0])
                                with open(save_path, 'wb') as f:
                                    f.write(pdf_content)
                                print(f"  -> 🎉 [打工人完工] 已从 ZIP 提取 PDF: {save_path.split('/')[-1]}")
                    except Exception as e:
                        print(f"  -> ❌ [打工人报错] ZIP 解压失败: {e}")

                elif raw_data.startswith(b'\x1f\x8b'):
                    print("  -> 🗜️ 检测到 GZIP 压缩，正在解压...")
                    import gzip
                    with open(save_path, 'wb') as f:
                        f.write(gzip.decompress(raw_data))
                    print(f"  -> 🎉 [打工人完工] GZIP 解压成功！")

                elif raw_data.startswith(b'%PDF'):
                    with open(save_path, 'wb') as f:
                        f.write(raw_data)
                    print(f"  -> 🎉 [打工人完工] 纯 PDF 直接落盘！")
                
                else:
                    print(f"  -> ❌ [打工人报错] 格式未知。前15字节: {raw_data[:15]}")
            else:
                print(f"  -> ❌ [打工人报错] 请求失败，状态码: {dl_res.status_code}")

        except Exception as e:
            print(f"  -> ❌ [打工人报错] 处理文件异常: {e}")
        finally:
            task_queue.task_done()

# ==========================================
# 👷 包工头：单一 Selenium 控制翻页与发布任务
# ==========================================
def jacar_auto_search(target_keyword, start_year, end_year, update_gui_progress, finish_scraping, stop_event):
    print("正在初始化网络环境与高并发队列...")
    
    if getattr(sys, 'frozen', False):
        application_path = os.path.dirname(sys.executable)
    else:
        application_path = os.path.dirname(os.path.abspath(__file__))

    download_dir = os.path.join(application_path, "JACAR_Downloads")
    if not os.path.exists(download_dir):
        os.makedirs(download_dir)
    
    task_queue = queue.Queue()
    
    workers = []
    for i in range(3):
        t = threading.Thread(target=api_download_worker, args=(task_queue, stop_event))
        t.daemon = True 
        t.start()
        workers.append(t)
    
    chrome_options = Options()
    driver = webdriver.Chrome(options=chrome_options)
    wait = WebDriverWait(driver, 15)
    
    # 👇 删掉外面的中括号和圆括号，只保留纯粹的网址！
    target_url = "https://www.jacar.archives.go.jp/aj/meta/default"
    
    try:
        driver.get(target_url)
        
        # ---------- 1. 执行搜索 ----------
        search_box = wait.until(EC.presence_of_element_located((By.ID, "searchbox")))
        search_box.clear() 
        search_box.send_keys(target_keyword)
        
        detail_p_tag = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".detail_bt1 p")))
        driver.execute_script("arguments[0].click();", detail_p_tag)
        time.sleep(1.5) 
        
        from_year_input = wait.until(EC.visibility_of_element_located((By.NAME, "from_yearS11")))
        from_year_input.clear()
        from_year_input.send_keys(start_year)
        
        to_year_input = wait.until(EC.visibility_of_element_located((By.NAME, "to_yearS11")))
        to_year_input.clear()
        to_year_input.send_keys(end_year)
        
        search_btn = driver.find_element(By.ID, "search_button")
        search_btn.click() 
        
        # ---------- 2. 遍历列表、翻页与分发任务 ----------
        print("等待搜索结果加载...")
        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "list_ti_txt")))
        
        current_page = 1
        total_tasks_added = 0   
        MAX_DOWNLOADS = 10

        while True:
            if stop_event.is_set() or total_tasks_added >= MAX_DOWNLOADS:
                break
            print(f"\n========== 🚀 正在火力全开扫荡第 {current_page} 页 ==========")
            
            wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(@class, 'schk_icon2')]")))
            number_icons = driver.find_elements(By.XPATH, "//span[contains(@class, 'schk_icon2')]")
            
            visible_numbers = [int(icon.text.strip()) for icon in number_icons if icon.is_displayed() and icon.text.strip().isdigit()]
                    
            if not visible_numbers:
                break
                
            start_no = min(visible_numbers) 
            end_no = max(visible_numbers)   
            total_items_this_page = end_no - start_no + 1

            for item_no in range(start_no, end_no + 1):
                if stop_event.is_set():
                    break
                current_progress = item_no - start_no + 1
                
                q_size = task_queue.qsize()
                update_gui_progress(
                    current_progress, 
                    total_items_this_page, 
                    f"正在踩点第 {current_page} 页 ({current_progress}/{total_items_this_page}) | 后台积压任务: {q_size} 份"
                )

                row_xpath = f"//dl[contains(@class, 'search_list') and .//span[contains(@class, 'schk_icon2') and text()='{item_no}']]"
                
                try:
                    current_block = driver.find_element(By.XPATH, row_xpath)
                    title_link = current_block.find_element(By.CLASS_NAME, "list_ti_txt")
                    doc_title = title_link.text
                    
                    print(f"\n▶️ [包工头踩点] {doc_title}")
                    main_window = driver.current_window_handle
                    driver.execute_script("arguments[0].click();", title_link)
                    
                    wait.until(EC.presence_of_element_located((By.XPATH, "//dt[contains(text(), 'レファレンスコード')]")))
                    try:
                        ref_code = driver.find_element(By.XPATH, "//dt[contains(text(), 'レファレンスコード')]/following-sibling::dd").text.strip()
                        hierarchy_links = driver.find_elements(By.XPATH, "//a[contains(@onclick, 'submitHierarchy')]")
                        level2_name = hierarchy_links[1].text if len(hierarchy_links) > 1 else hierarchy_links[0].text
                        parent_name = hierarchy_links[-1].text if len(hierarchy_links) > 0 else "未知父级"
                        repo_raw_text = driver.find_element(By.XPATH, "//dt[contains(text(), '所蔵館における請求番号')]/following-sibling::dd").text.strip()
                        repo_name = repo_raw_text
                        if "(" in repo_name: repo_name = repo_name.split("(")[-1].replace(")", "")
                        elif "（" in repo_name: repo_name = repo_name.split("（")[-1].replace("）", "")
                        if "外務省外交史料館" in repo_name: repo_name = "日本外交史料館"
                    except Exception:
                        ref_code, level2_name, parent_name, repo_name = "Unknown_Ref", "未知层级", "未知分类", "未知馆藏"
                
                    view_btn = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "view_bt1")))
                    driver.execute_script("arguments[0].click();", view_btn)

                    raw_target_name = f"{level2_name}：「{doc_title}」、JACAR Ref. {ref_code}（第（）—（）画像目）、『{parent_name}』（{repo_name}）"
                    safe_target_name = re.sub(r'[\\/:*?"<>|]', lambda x: {'\\': '＼', '/': '／', ':': '：', '*': '＊', '?': '？', '"': '”', '<': '＜', '>': '＞', '|': '｜'}[x.group()], raw_target_name)
                    
                    final_save_path = os.path.join(download_dir, safe_target_name + ".pdf")

                    wait.until(lambda d: len(d.window_handles) > 1)
                    driver.switch_to.window(driver.window_handles[-1])
                    
                    viewer_dl_btn = wait.until(EC.presence_of_element_located((By.XPATH, "//a[contains(@href, 'changeFormat')]")))
                    href_content = viewer_dl_btn.get_attribute("href") 
                    parts = href_content.split("'")
                    bid_val = parts[3]  
                    id_val = parts[5]   
                    
                    current_viewer_url = driver.current_url
                    if "digital.archives.go.jp" in current_viewer_url:
                        target_jump_url = f"https://www.digital.archives.go.jp/DAS/meta/listPhoto?LANG=default&BID={bid_val}&ID={id_val}&NO=&TYPE=dljpeg&DL_TYPE=pdf"
                    else:
                        target_jump_url = f"https://www.jacar.archives.go.jp/aj/meta/listPhoto?LANG=default&REFCODE={ref_code}&BID={bid_val}&ID={id_val}&NO=&TYPE=dljpeg&DL_TYPE=pdf"
                        
                    task_queue.put({
                        "url": target_jump_url,
                        "save_path": final_save_path,
                        "title": doc_title,
                        "vip_cookies": driver.get_cookies(), 
                        "user_agent": driver.execute_script("return navigator.userAgent;") 
                    })
                    total_tasks_added += 1
                    print(f"  -> 📦 任务已打包！(当前积压: {task_queue.qsize()} 个) | 测试总进度: {total_tasks_added}/{MAX_DOWNLOADS}")
                    
                    if total_tasks_added >= MAX_DOWNLOADS:
                        print(f"\n🎯 已经达到测试限制的 {MAX_DOWNLOADS} 份，包工头停止踩点！")
                        break
                except Exception as row_e:
                    error_msg = str(row_e).split('\n')[0] 
                    print(f"  -> ⚠️ 无法处理序号 [{item_no}]，已被拦截或元素超时。简略报错: {error_msg}")
                    continue
                finally:
                    if len(driver.window_handles) > 1:
                        driver.close()
                    driver.switch_to.window(main_window)
                    driver.back()
                    wait.until(EC.presence_of_element_located((By.CLASS_NAME, "list_ti_txt")))
                    time.sleep(1) 

            try:
                if total_tasks_added >= MAX_DOWNLOADS:
                    break
                next_btn = driver.find_element(By.XPATH, "//a[contains(@class, 'page_bt') and contains(text(), '次')]")
                print(f"\n👉 第 {current_page} 页已清空，准备翻页...")
                driver.execute_script("arguments[0].click();", next_btn)
                time.sleep(3) 
                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "list_ti_txt")))
                current_page += 1
            except Exception:
                print("\n🛑 没有找到【下一页】按钮，说明所有页面的史料已被彻底翻完！")
                break
        
        # ==========================================
        # 🏁 包工头工作结束，等待打工人下班
        # ==========================================
        if not stop_event.is_set():
            update_gui_progress(0, 0, "⏳ 网页已翻完，正在等待后台线程下载剩余史料...")
            print("\n⏳ 正在等待后台打工人清理积压任务...")
            while not task_queue.empty() or task_queue.unfinished_tasks > 0:
                if stop_event.is_set():
                    break
                time.sleep(1)

        if stop_event.is_set():
            finish_scraping("🛑 任务已被手动终止。部分文件可能未下载。")
        else:
            finish_scraping("🎉 高并发自动化抓取圆满完成！请检查文件夹！")
        
    except Exception as e:
        print("====== 🚨 发生致命错误 ======")
        traceback.print_exc()
        finish_scraping("❌ 发生致命错误，请查看终端日志。")
        
    finally:
        time.sleep(2)
        try:
            driver.quit()
        except:
            pass
"""简易网页搜索工具（替代 web_search，不需要 API Key）"""
import re, urllib.parse
import requests as rq
from config import PROXY

prox = {'http': PROXY, 'https': PROXY}
headers = {
    'User-Agent': 'Mozilla/5.0',
    'Accept': 'text/html',
}


def search(query: str, max_results: int = 5) -> list:
    """搜索网页，返回 [{title, url, snippet}]"""
    # 备用搜索源列表
    sources = [
        _search_bing,  # Google 需要JS，放弃
    ]
    
    all_results = []
    for source in sources:
        try:
            results = source(query, max_results)
            if results:
                all_results.extend(results)
                if len(all_results) >= max_results:
                    return all_results[:max_results]
        except:
            continue
    
    return all_results


def _search_google(query: str, max_results: int = 5) -> list:
    """从 Google 搜索"""
    url = f"https://www.google.com/search?q={urllib.parse.quote(query)}&hl=en"
    r = rq.get(url, proxies=prox, headers=headers, timeout=12)
    if r.status_code != 200:
        return []
    
    html = r.text
    results = []
    
    # 提取搜索结果
    # Google 使用 <div class="g"> 包裹每个结果
    # 标题在 <h3> 里，链接在 <a href="..."> 里，摘要有时在 <div class="VwiC3b"> 里
    blocks = re.findall(r'<div[^>]*class="[^"]*g[^"]*"[^>]*>(.*?)</div>\s*</div>\s*</div>', html, re.DOTALL)
    
    for block in blocks[:max_results]:
        # 标题
        title_match = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.DOTALL)
        title = re.sub(r'<[^>]+>', '', title_match.group(1)) if title_match else ''
        
        # 链接
        url_match = re.search(r'href="(https?://[^"]+)"', block)
        url_str = urllib.parse.unquote(url_match.group(1)) if url_match else ''
        
        # 摘要
        snippet_match = re.search(r'class="[^"]*VwiC3b[^"]*"[^>]*>(.*?)</div>', block, re.DOTALL)
        snippet = re.sub(r'<[^>]+>', '', snippet_match.group(1)) if snippet_match else ''
        
        if title:
            results.append({"title": title, "url": url_str, "snippet": snippet[:200]})
    
    return results


def _search_bing(query: str, max_results: int = 5) -> list:
    """从 Bing 搜索"""
    url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
    r = rq.get(url, proxies=prox, headers=headers, timeout=12)
    if r.status_code != 200:
        return []
    
    html = r.text
    results = []
    
    # Bing 使用 <li class="b_algo"> 包裹
    blocks = re.findall(r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>', html, re.DOTALL)
    
    for block in blocks[:max_results]:
        title_match = re.search(r'<h2[^>]*><a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        if title_match:
            url_str = title_match.group(1)
            # Bing 用 /ck/a 跳转链接，提取真实URL
            if 'bing.com/ck/a' in url_str or 'bing.com' in url_str:
                m = re.search(r'u=a1([a-zA-Z0-9+/]+=*)', url_str)
                if m:
                    import base64
                    try:
                        decoded = base64.b64decode(m.group(1).replace('-', '+').replace('_', '/')).decode()
                        # 去掉协议前缀（如果有多个）
                        if decoded.startswith('a1'):
                            decoded = decoded[2:]
                        url_str = urllib.parse.unquote(decoded)
                    except:
                        pass
            title = re.sub(r'<[^>]+>', '', title_match.group(2))
            snippet_match = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
            snippet = re.sub(r'<[^>]+>', '', snippet_match.group(1)) if snippet_match else ''
            if title:
                results.append({"title": title, "url": url_str, "snippet": snippet[:200]})
    
    return results


def fetch_page(url: str, max_chars: int = 5000) -> str:
    """获取页面内容（纯文本）"""
    try:
        r = rq.get(url, proxies=prox, headers=headers, timeout=12)
        if r.status_code == 200:
            text = re.sub(r'<script[^>]*>.*?</script>', '', r.text, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return text[:max_chars]
    except:
        pass
    return ""


if __name__ == "__main__":
    import sys
    q = ' '.join(sys.argv[1:]) or "crypto scalping strategy"
    print(f"搜索: {q}\n")
    results = search(q, 5)
    for r in results:
        print(f"• {r['title']}")
        print(f"  {r['url']}")
        if r['snippet']:
            print(f"  {r['snippet'][:120]}")
        print()

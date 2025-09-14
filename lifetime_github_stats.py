#!/usr/bin/env python3
"""
GitHub Code Statistics Analyzer
Analyzes total lines of code contributed across all GitHub repositories.
"""

import requests
import time
import os
import json
from datetime import datetime
from typing import Dict, List, Tuple, Optional
import sys

# Configuration
GITHUB_USERNAME = ""
GITHUB_TOKEN = ""  # ignore
API_BASE = "https://api.github.com"
CACHE_FILE = "github_stats_cache.json"
RATE_LIMIT_DELAY = 1.0  # seconds between requests
STATS_RETRY_DELAY = 5.0  # seconds to wait when stats are being computed
MAX_STATS_RETRIES = 6  # maximum retries for stats APIs (30 seconds total)

class GitHubStatsAnalyzer:
    def __init__(self, username: str, token: str):
        self.username = username
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        self.cache = self._load_cache()
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        
    def _load_cache(self) -> Dict:
        """Load cached data if available."""
        try:
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            print(f"Warning: Could not load cache: {e}")
        return {}
    
    def _save_cache(self):
        """Save cache data."""
        try:
            with open(CACHE_FILE, 'w') as f:
                json.dump(self.cache, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not save cache: {e}")
    
    def _make_request(self, url: str, params: Optional[Dict] = None, allow_non_200: bool = False) -> Optional[requests.Response]:
        """Make a rate-limited API request with error handling."""
        try:
            response = self.session.get(url, params=params)
            
            # Handle rate limiting
            if response.status_code == 403 and 'rate limit' in response.text.lower():
                reset_time = int(response.headers.get('X-RateLimit-Reset', 0))
                wait_time = max(reset_time - int(time.time()), 60)
                print(f"Rate limit hit. Waiting {wait_time} seconds...")
                time.sleep(wait_time)
                return self._make_request(url, params, allow_non_200)
            
            # For stats APIs, we need to handle 202 and 204 specially
            if allow_non_200:
                time.sleep(RATE_LIMIT_DELAY)
                return response
            elif response.status_code == 200:
                time.sleep(RATE_LIMIT_DELAY)
                return response
            else:
                return None
                
        except Exception as e:
            print(f"    Request error for {url}: {e}")
            return None
    
    def _get_paginated_data(self, url: str, params: Optional[Dict] = None) -> List[Dict]:
        """Get all pages of data from a paginated endpoint."""
        results = []
        page = 1
        per_page = 100
        
        while True:
            current_params = {"page": page, "per_page": per_page}
            if params:
                current_params.update(params)
                
            response = self._make_request(url, current_params)
            if not response:
                break
                
            data = response.json()
            if not data or len(data) == 0:
                break
                
            results.extend(data)
            page += 1
            
            # Print progress for large result sets
            if page > 2:  # Only show progress for multi-page results
                print(f"  Fetched page {page-1}, total items: {len(results)}")
            
        return results

    def get_all_repositories(self) -> List[Dict]:
        """Get all repositories accessible to the user."""
        print("Fetching personal repositories...")
        repos = self._get_paginated_data(f"{API_BASE}/user/repos", {"type": "all", "sort": "updated"})
        
        print("Fetching organization repositories...")
        orgs_response = self._make_request(f"{API_BASE}/user/orgs")
        if orgs_response:
            orgs = orgs_response.json()
            for org in orgs:
                print(f"  Fetching repos for organization: {org['login']}")
                org_repos = self._get_paginated_data(f"{API_BASE}/orgs/{org['login']}/repos", {"type": "all"})
                repos.extend(org_repos)
        
        # Remove duplicates based on full_name
        seen = set()
        unique_repos = []
        for repo in repos:
            if repo['full_name'] not in seen:
                seen.add(repo['full_name'])
                unique_repos.append(repo)
        
        print(f"Found {len(unique_repos)} unique repositories")
        return unique_repos
    
    def get_repository_stats(self, repo: Dict) -> Tuple[int, int]:
        """Get contribution stats for a specific repository."""
        repo_name = repo['full_name']
        cache_key = f"repo_stats_{repo_name}_v2"  # v2 to invalidate old cache
        
        # Check cache first (cache for 24 hours)
        if cache_key in self.cache:
            cached_data = self.cache[cache_key]
            if time.time() - cached_data.get('timestamp', 0) < 86400:  # 24 hours
                return cached_data['additions'], cached_data['deletions']
        
        print(f"  Analyzing: {repo_name}")
        
        # Try multiple approaches in order of preference
        additions, deletions = self._get_stats_from_contributors_with_retry(repo_name)
        
        if additions == 0 and deletions == 0:
            additions, deletions = self._get_stats_from_code_frequency(repo_name)
        
        if additions == 0 and deletions == 0:
            additions, deletions = self._get_stats_from_commits_sample(repo_name)
        
        # Cache the results
        self.cache[cache_key] = {
            'additions': additions,
            'deletions': deletions,
            'timestamp': time.time()
        }
        
        return additions, deletions
    
    def _get_stats_from_contributors_with_retry(self, repo_name: str) -> Tuple[int, int]:
        """Get stats from contributors API with proper retry logic for 202 responses."""
        url = f"{API_BASE}/repos/{repo_name}/stats/contributors"
        
        for attempt in range(MAX_STATS_RETRIES):
            response = self._make_request(url, allow_non_200=True)
            if not response:
                return 0, 0
            
            if response.status_code == 200:
                try:
                    contributors = response.json()
                    if not contributors:
                        return 0, 0
                    
                    for contributor in contributors:
                        author = contributor.get('author')
                        if author and author.get('login') == self.username:
                            total_additions = sum(week.get('a', 0) for week in contributor.get('weeks', []))
                            total_deletions = sum(week.get('d', 0) for week in contributor.get('weeks', []))
                            print(f"    Found contributor data: +{total_additions:,} -{total_deletions:,}")
                            return total_additions, total_deletions
                    
                    return 0, 0  # User not found in contributors
                    
                except Exception as e:
                    print(f"    Error parsing contributors data: {e}")
                    return 0, 0
            
            elif response.status_code == 202:
                print(f"    Stats computing... waiting {STATS_RETRY_DELAY}s (attempt {attempt+1}/{MAX_STATS_RETRIES})")
                time.sleep(STATS_RETRY_DELAY)
                continue
            
            elif response.status_code == 204:
                print(f"    No contributor data available")
                return 0, 0
            
            elif response.status_code == 422:
                print(f"    Repository too large (10k+ commits), trying alternative method")
                return 0, 0
            
            else:
                print(f"    API Error {response.status_code}: {response.text[:100]}...")
                return 0, 0
        
        print(f"    Stats API timeout after {MAX_STATS_RETRIES} attempts")
        return 0, 0
    
    def _get_stats_from_code_frequency(self, repo_name: str) -> Tuple[int, int]:
        """Get stats from code frequency API (repository-wide, then filter by commits)."""
        url = f"{API_BASE}/repos/{repo_name}/stats/code_frequency"
        
        for attempt in range(MAX_STATS_RETRIES):
            response = self._make_request(url, allow_non_200=True)
            if not response:
                return 0, 0
            
            if response.status_code == 200:
                try:
                    frequency_data = response.json()
                    if not frequency_data:
                        return 0, 0
                    
                    # This gives us total repo stats, but we need to check if user contributed
                    # Let's get user's commits to see if they contributed at all
                    commits = self._get_user_commits_sample(repo_name)
                    if not commits:
                        return 0, 0
                    
                    # If user has commits, estimate their contribution
                    # This is rough but better than nothing
                    total_additions = sum(week[1] for week in frequency_data if len(week) >= 2)
                    total_deletions = abs(sum(week[2] for week in frequency_data if len(week) >= 3))
                    
                    # Scale by user's commit percentage (rough estimate)
                    user_commits = len(commits)
                    if user_commits > 0:
                        # Get total commits (sample)
                        total_commits_response = self._make_request(f"{API_BASE}/repos/{repo_name}/commits", {"per_page": 1})
                        if total_commits_response:
                            # This is a very rough estimation
                            scaling_factor = min(user_commits / 100, 0.5)  # Cap at 50%
                            estimated_additions = int(total_additions * scaling_factor)
                            estimated_deletions = int(total_deletions * scaling_factor)
                            print(f"    Estimated from code frequency: +{estimated_additions:,} -{estimated_deletions:,}")
                            return estimated_additions, estimated_deletions
                    
                    return 0, 0
                    
                except Exception as e:
                    print(f"    Error parsing code frequency data: {e}")
                    return 0, 0
            
            elif response.status_code == 202:
                print(f"    Code frequency computing... waiting {STATS_RETRY_DELAY}s")
                time.sleep(STATS_RETRY_DELAY)
                continue
            
            elif response.status_code == 204:
                return 0, 0
            
            else:
                return 0, 0
        
        return 0, 0
    
    def _get_user_commits_sample(self, repo_name: str) -> List[Dict]:
        """Get a sample of user's commits from the repository."""
        try:
            commits_url = f"{API_BASE}/repos/{repo_name}/commits"
            response = self._make_request(commits_url, {"author": self.username, "per_page": 10})
            if response:
                return response.json()
        except:
            pass
        return []
    
    def _get_stats_from_commits_sample(self, repo_name: str) -> Tuple[int, int]:
        """Get stats by analyzing a sample of individual commits."""
        commits_url = f"{API_BASE}/repos/{repo_name}/commits"
        commits = self._get_user_commits_sample(repo_name)
        
        if not commits:
            return 0, 0
        
        total_additions = 0
        total_deletions = 0
        
        print(f"    Sampling {len(commits)} recent commits...")
        
        for i, commit in enumerate(commits[:10]):  # Limit to 10 commits to avoid rate limits
            sha = commit['sha']
            commit_response = self._make_request(f"{API_BASE}/repos/{repo_name}/commits/{sha}")
            
            if commit_response:
                commit_data = commit_response.json()
                stats = commit_data.get('stats', {})
                additions = stats.get('additions', 0)
                deletions = stats.get('deletions', 0)
                total_additions += additions
                total_deletions += deletions
        
        if total_additions > 0 or total_deletions > 0:
            print(f"    Sample analysis: +{total_additions:,} -{total_deletions:,} (from {len(commits)} commits)")
        
        return total_additions, total_deletions
    
    def analyze_all_repositories(self):
        """Analyze all repositories and return total stats."""
        repos = self.get_all_repositories()
        
        total_additions = 0
        total_deletions = 0
        processed_repos = 0
        repos_with_contributions = []
        
        print(f"\nAnalyzing contributions across {len(repos)} repositories...")
        print("=" * 80)
        
        for i, repo in enumerate(repos, 1):
            try:
                print(f"\n[{i}/{len(repos)}] {repo['full_name']}")
                
                # Skip forks unless you want to include them
                if repo.get('fork', False):
                    print("  Skipping fork")
                    continue
                
                # Skip empty repositories
                if repo.get('size', 0) == 0:
                    print("  Skipping empty repository")
                    continue
                
                additions, deletions = self.get_repository_stats(repo)
                
                if additions > 0 or deletions > 0:
                    print(f"  ✅ +{additions:,} -{deletions:,} lines")
                    total_additions += additions
                    total_deletions += deletions
                    processed_repos += 1
                    repos_with_contributions.append({
                        'name': repo['full_name'],
                        'additions': additions,
                        'deletions': deletions
                    })
                else:
                    print("  ⭕ No contributions found")
                    
            except Exception as e:
                print(f"  ❌ Error processing {repo['full_name']}: {e}")
                continue
        
        # Save cache after processing
        self._save_cache()
        
        # Display results
        print("\n" + "=" * 80)
        print("FINAL RESULTS")
        print("=" * 80)
        print(f"Repositories analyzed: {processed_repos}/{len(repos)}")
        print(f"Total additions: {total_additions:,}")
        print(f"Total deletions: {total_deletions:,}")
        print(f"Net lines of code: {total_additions - total_deletions:,}")
        
        if repos_with_contributions:
            print(f"\nRepositories with contributions:")
            for repo_info in sorted(repos_with_contributions, key=lambda x: x['additions'], reverse=True):
                net = repo_info['additions'] - repo_info['deletions']
                print(f"  {repo_info['name']}: +{repo_info['additions']:,} -{repo_info['deletions']:,} (net: {net:+,})")
        
        print(f"\nAnalysis completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        return total_additions, total_deletions

def main():
    """Main function."""
    # Check for GitHub token
    if not GITHUB_TOKEN:
        print("Error: Please set the GITHUB_TOKEN environment variable")
        print("You can get a token from: https://github.com/settings/tokens")
        print("Required scopes: repo (for private repos) or public_repo (for public repos only)")
        sys.exit(1)
    
    print("GitHub Code Statistics Analyzer")
    print(f"Analyzing contributions for user: {GITHUB_USERNAME}")
    print(f"Using cache file: {CACHE_FILE}")
    print("-" * 80)
    
    analyzer = GitHubStatsAnalyzer(GITHUB_USERNAME, GITHUB_TOKEN)
    
    try:
        analyzer.analyze_all_repositories()
    except KeyboardInterrupt:
        print("\n\nAnalysis interrupted by user")
        analyzer._save_cache()
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        analyzer._save_cache()

if __name__ == "__main__":
    main()

# SBS OnDemand Downloader

An alternative way to view SBS OnDemand content. No need create an account or install flash player.


## Dependencies

Requires Python 3.5 or higher.

* ffmpeg-python
* pycurl
* m3u8
* jsonfinder
* lxml


## Installation

Use `pip` to install all of the dependencies:
```
pip install -r requirements.txt
```


## Usage

1. Fetch the most up-to-date list of titles from SBS OnDemand to populate the application database.
```commandline
python sbs_ondemand.py sync
```

2. Search for a title to download. All available episodes will be retrieved in the case of a TV series. If the
search term is too broad, multiple results will be found and nothing will be downloaded so narrow your search.

```commandline
python sbs_ondemand.py download "show/movie title" 
```

3. View help for additional usage:
```commandline
python sbs_ondemand.py -h
```

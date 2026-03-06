# fedora-repository
This is my custom fedora repository for hosting multiple services. None of these services are build by me, they are simple downloeaded from official sources and added to this repo so that they can be installed through dnf and allowed to auto update

I created this repository for my personal use, however everyone is welcome to use it themselves if they want to.
If not, I'm providing all scripts and explanation on how I created the repos so that they can be replicated and self hosted.

### 1. Principle of Work
**Install Dependencies**

`sudo dnf install createrepo httpd`

**Create repository folder structure**

`sudo mkdir -p /var/www/html/<repo>`

**Start Apache server**

`sudo systemctl enable --now httpd`

-----------
### 2. Helper scripts

The helper scripts are all located in the `root/<repo>` folder.
Each folder contains both the python _download_ script and the _update_ bash script. 

-----------
### 3. Cron Job
The bash scripts are scheduled to run every week via a cron job.
The purpose of the bash scripts is to:
1. run the python download scripts, passing the appropriate arguments
2. prune the downloaded files to keep only the last 3 versions
3. sign the packages
4. update the repository metadata.

-----------
### 4. Signing the packages

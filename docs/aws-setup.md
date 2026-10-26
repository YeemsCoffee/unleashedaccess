# Setting up Unleashed Access on AWS, step by step

What you'll build:

```
Your team ──HTTPS──► Load balancer ──► Elastic Beanstalk instance (the app + background sync)
                                              │                         │
                                              ▼                         ▼
                                  RDS PostgreSQL database      api.unleashedsoftware.com
```

It takes about an hour, mostly waiting for AWS to create things. Do the parts in order.

| Part | What | Time |
|------|------|------|
| 0 | Pick a region, gather what you need | 5 min |
| 1 | Two security groups (firewall rules) | 5 min |
| 2 | The database (RDS PostgreSQL) | 15 min, mostly waiting |
| 3 | The code package (a zip) | 2 min |
| 4 | The app (Elastic Beanstalk) | 20 min, mostly waiting |
| 5 | HTTPS and your own web address | 15 min |
| 6 | Sign in and check the data | 5 min |

Console labels move around occasionally; if a field isn't exactly where described,
look for the same name nearby on that page.

---

## Part 0: Before you start

1. **Pick one region and use it for everything.** Use the region your other apps
   run in (shown top-right in the AWS console, e.g. *US West (Oregon) us-west-2*).
   The database, the app and the HTTPS certificate must all be in the same region.
2. **Have these ready** (a password manager is a good place for them):
   - Your Unleashed **API ID** and **API key**.
   - A **database password** you make up: 16+ characters. Avoid `/`, `@`, `"`, `'`
     and spaces; RDS rejects those.
   - A **temporary password for your own account** (10+ characters). You'll replace
     it with your own the first time you sign in. Everyone on your team gets their own
     account later, from inside the app; there's no shared dashboard password.
3. **Know which VPC your apps use.** Most accounts use the *default VPC*. If your
   company has its own VPC, use that, and make sure it has **public subnets** (or a
   NAT gateway), because the app must reach Unleashed on the internet.

---

## Part 1: Security groups

A security group is a firewall. You'll make one for the app and one for the
database that lets **only the app** connect to it.

1. Open the **VPC** console → left menu **Security groups** → **Create security group**.
2. First group, for the app:
   - **Name:** `unleashedaccess-app`
   - **Description:** `Unleashed Access app servers`
   - **VPC:** your VPC
   - **Inbound rules:** none.
   - **Outbound rules:** keep the default **All traffic → 0.0.0.0/0** rule. The app
     needs it to reach Unleashed, the database, and Python packages while installing.
   - **Create security group**.
3. Back to **Security groups** → **Create security group** again, for the database:
   - **Name:** `unleashedaccess-db`
   - **Description:** `Unleashed Access database`
   - **VPC:** the same VPC
   - **Inbound rules → Add rule:** Type **PostgreSQL** (port 5432 fills in),
     Source **Custom**, then pick **`unleashedaccess-app`** from the list.
   - **Outbound rules:** keep the default. (Security groups always let a database
     answer connections it accepted, so this rule doesn't matter either way.)
   - **Create security group**.

✅ Checkpoint: two security groups exist, and `unleashedaccess-db` has one inbound
rule allowing PostgreSQL from `unleashedaccess-app`.

---

## Part 2: The database (Amazon RDS for PostgreSQL)

1. Open the **RDS** console → **Databases** → **Create database**.
2. **Choose a database creation method:** **Standard create**.
3. **Engine options:** the box labeled just **PostgreSQL**, not *Aurora (PostgreSQL
   Compatible)*. If you see **Cluster scalability type**, Aurora is selected; switch
   back. Aurora works with the app too, but its smallest setup costs several times
   more for no benefit at this size. Engine version: any of **16**, **17** or **18**;
   the newest (e.g. **18.3**) is a good choice. The app is tested on 16 and 18.3.
4. **Templates:** **Free tier** if offered, otherwise **Dev/Test**.
   (Don't pick *Production*: it defaults to two servers and costs several times more.)
5. **Availability and durability** (if shown): **Single-AZ DB instance deployment**,
   which some forms call **Do not create a standby instance**. A standby roughly
   doubles the cost and isn't needed: the database is a copy of Unleashed data, RDS
   still takes daily backups, and a full sync can rebuild it.
6. **Settings:**
   - **DB instance identifier:** `unleashedaccess-db`
   - **Master username:** `unleashed_admin`
   - **Credentials management:** **Self managed** — not *Managed in AWS Secrets
     Manager*. Secrets Manager rotates the password every 7 days by default, and the
     app would lose access when it did.
   - **Master password** / **Confirm:** the database password from Part 0.
7. **Instance configuration:** **Burstable classes** → **db.t4g.micro**.
8. **Storage:** **gp3**, **20** GiB. Leave storage autoscaling on.
9. **Connectivity:**
   - **Compute resource:** **Don't connect to an EC2 compute resource**.
   - **VPC:** your VPC. **DB subnet group:** the default is fine.
   - **Public access:** **No**.
   - **VPC security group:** **Choose existing** → select **`unleashedaccess-db`**,
     and remove **default** from the list. If it isn't in the list, you haven't done
     Part 1 yet: leave this form open, do Part 1 in a new browser tab, then come back
     and reopen the dropdown (reload the page if it still doesn't appear).
   - **Database port** (under *Additional configuration*): **5432**.
10. **Database authentication:** **Password authentication**.
11. **Monitoring:** keep it to the free basics. **Database Insights: Standard**;
    **Performance Insights** on with **7 days** retention if it's already on (free);
    **Enhanced Monitoring: off**; no **log exports**; **DevOps Guru: off**. The app
    reports its own sync health, and Elastic Beanstalk shows the app's health.
12. Open **Additional configuration** (near the bottom):
    - **Initial database name:** `unleashedaccess` ← easy to miss, and needed.
    - **Backup retention:** 7 days (default) is plenty. The database only holds a
      copy of your Unleashed data, so a full sync can always rebuild it.
    - **Enable deletion protection:** ✔ (recommended).
13. **Create database.** It takes 5–15 minutes. Wait for **Status: Available**.
14. Click **`unleashedaccess-db`** → **Connectivity & security** tab → copy the
    **Endpoint**, which looks like
    `unleashedaccess-db.abc123xyz.us-west-2.rds.amazonaws.com`.

✅ Checkpoint: status **Available**, and you've saved the endpoint.

---

## Part 3: The code package

On your computer, in the `unleashedaccess` folder:

```
git pull
git archive -o unleashedaccess.zip HEAD
```

That makes `unleashedaccess.zip` with the code at the top level of the zip,
including the hidden `.ebextensions` folder that sets the health check and time
zone. Your `.env` file (with your keys) is **not** included, which is what you want.

---

## Part 4: The app (Elastic Beanstalk)

1. Open the **Elastic Beanstalk** console → **Create application**
   (or **Create environment** if you already have EB applications).

**Step 1 — Configure environment**

2. **Environment tier:** **Web server environment**.
3. **Application name:** `unleashedaccess`.
4. **Environment name:** `unleashedaccess-prod`. **Domain:** optionally pick a
   prefix such as `yeems-unleashed` (you'll use your own address in Part 5 anyway).
5. **Platform:** **Managed platform** → **Python** → branch **Python 3.12 running
   on 64bit Amazon Linux 2023** (the newest Python 3.x branch also works) → recommended version.
6. **Application code:** **Upload your code** → **Version label** `v1` →
   **Local file** → choose `unleashedaccess.zip`.
7. **Presets:** **Custom configuration** (you'll pick the load balancer next) → **Next**.

**Step 2 — Configure service access**

8. **Service role:** **Use an existing service role** → `aws-elasticbeanstalk-service-role`
   if it exists, otherwise **Create and use new service role**.
9. **EC2 instance profile:** `aws-elasticbeanstalk-ec2-role` (or the profile your
   other EB apps use). If the list is empty, create it in another tab:
   **IAM** → **Roles** → **Create role** → trusted entity **AWS service**, use case
   **EC2** → attach the policy **AWSElasticBeanstalkWebTier** → name it
   `aws-elasticbeanstalk-ec2-role` → back here, refresh the list and pick it.
10. **EC2 key pair:** leave empty → **Next**.

**Step 3 — Set up networking, database, and tags**

11. **VPC:** the same VPC as the database.
12. **Instance settings:** **Public IP address: Activated**, and tick the **public
    subnets** (in the default VPC, all of them). The app needs this to reach Unleashed.
13. **Database:** leave **off**. You already made the database in Part 2.
14. **Next**.

**Step 4 — Configure instance traffic and scaling**

15. **EC2 security groups:** tick **`unleashedaccess-app`**. This is what lets the
    app into the database.
16. **Capacity → Environment type:** **Load balanced**. **Instances:** Min **1**, Max **1**.
17. **Instance types:** **t3.small** (remove any others listed).
18. **Load balancer network settings:** visibility **Public**, and tick the same
    public subnets as the instances (at least two). (Some console versions show this
    on the previous page instead.)
19. **Load balancer type:** **Application load balancer**, **Dedicated**.
20. **Processes:** select **default** → **Edit** → **Health check path:** `/healthz`
    → **Save**.
21. **Next**.

**Step 5 — Configure updates, monitoring, and logging**

22. **Health reporting:** **Enhanced** (default).
23. **Log streaming** (under *Instance log streaming to CloudWatch Logs*):
    **Activated**, retention **7 days**. Handy if you need to troubleshoot.
24. **Environment properties** (at the bottom) → **Add environment property** for each:

    | Name | Value |
    |------|-------|
    | `DB_HOST` | the RDS endpoint from Part 2 |
    | `DB_NAME` | `unleashedaccess` |
    | `DB_USER` | `unleashed_admin` |
    | `DB_PASSWORD` | the database password |
    | `UNLEASHED_API_ID` | your Unleashed API ID |
    | `UNLEASHED_API_KEY` | your Unleashed API key |
    | `ADMIN_EMAIL` | your email: you'll sign in with it as the first admin |
    | `ADMIN_NAME` | your name, as the dashboard shows it |
    | `ADMIN_PASSWORD` | the temporary password from Part 0 |
    | `TIMEZONE` | `America/Los_Angeles` (may already be filled in) |

25. **Next** → **Review** → **Submit**. Creation takes 5–15 minutes.

✅ Checkpoint: the environment's **Health** turns **Green / Ok**. Open
`http://<your environment domain>/healthz`; it should show `{"status":"ok"}`.

Don't sign in yet: until Part 5 the site is plain HTTP, so the password would
travel unencrypted. (The first copy of your Unleashed data is already running in
the background.)

**If you get a 502 or health turns Red:** open the environment's address. If the
app can't start, it shows a page saying why (and retries every 30 seconds). The
same reason is in **Logs** → **Request logs** → **Last 100 lines**. Common ones:

| You see | Meaning and fix |
|---------|-----------------|
| `can't reach the database` / `timeout expired` / `couldn't get a connection` | The network path is blocked. Check: the instance has the `unleashedaccess-app` security group (EC2 → Instances → the instance → **Security** tab; if missing, step 15); the `unleashedaccess-db` inbound rule's source is `unleashedaccess-app`; the database uses `unleashedaccess-db`; and the database and app are in the same VPC and region. |
| `DB_HOST isn't an address the app can find` | `DB_HOST` must be the RDS endpoint exactly, without `https://` or `:5432`. |
| `password authentication failed` | `DB_USER` or `DB_PASSWORD` doesn't match the database. |
| `database "…" does not exist` | `DB_NAME` doesn't match the **Initial database name**. |
| `Unleashed … Forbidden` (on the Connection page later) | `UNLEASHED_API_ID` / `UNLEASHED_API_KEY` is wrong. |

After fixing environment properties, click **Apply**; EB restarts the app by itself.

---

## Part 5: HTTPS and your own web address

You need a domain name you control, e.g. `dashboard.yeemscoffee.com`.

**A. Get a certificate (AWS Certificate Manager)** — in the **same region**:

1. **Certificate Manager** → **Request** → **Request a public certificate** → **Next**.
2. **Fully qualified domain name:** `dashboard.yeemscoffee.com` (your choice).
   **Validation method:** **DNS validation** → **Request**.
3. Open the certificate. Under **Domains**:
   - If your domain's DNS is in **Route 53**: click **Create records in Route 53** → **Create records**.
   - If it's elsewhere (GoDaddy, Cloudflare, Squarespace…): add the **CNAME name**
     and **CNAME value** it shows as a CNAME record at your DNS provider.
4. Wait until **Status: Issued** (usually 5–30 minutes).

**B. Add HTTPS to the load balancer:**

5. **Elastic Beanstalk** → `unleashedaccess-prod` → **Configuration** →
   **Instance traffic and scaling** → **Edit**.
6. **Listeners** → **Add listener**: Port **443**, Protocol **HTTPS**, SSL
   certificate: the one from step 4, SSL policy: the default → **Save**.
7. Turn off the **port 80** listener (select it, then disable it), so nobody signs
   in over plain HTTP → **Apply** at the bottom of the page.

**C. Point your address at the app:**

8. - **Route 53:** **Hosted zones** → your domain → **Create record** → Record name
     `dashboard` → **Alias** on → **Alias to Elastic Beanstalk environment** → your
     region → your environment → **Create records**.
   - **Other DNS provider:** add a **CNAME** record `dashboard` → your environment's
     domain (e.g. `yeems-unleashed.us-west-2.elasticbeanstalk.com`).

**D. Lock the sign-in cookie to HTTPS:**

9. **Configuration** → **Updates, monitoring, and logging** → **Edit** →
   **Environment properties** → add `SECURE_COOKIES` = `1` → **Apply**.

✅ Checkpoint: `https://dashboard.yeemscoffee.com/healthz` shows `{"status":"ok"}`
with a padlock in the browser.

---

## Part 6: Sign in and check the data

1. Open `https://dashboard.yeemscoffee.com` and sign in with your `ADMIN_EMAIL` and
   the temporary `ADMIN_PASSWORD`. The app asks you to choose your own password
   straight away.
2. Remove `ADMIN_PASSWORD` from the environment properties (**Configuration** →
   **Updates, monitoring, and logging** → **Edit** → remove it → **Apply**). It no
   longer works anyway, but there's no reason to keep it there.
3. If the first copy is still running you'll see its progress. It takes a minute or
   two (longer if Unleashed needs one request per purchase order for cost lines).
4. Go to **Connection** → **Check live data**. Every row should say **✓ OK**. Compare
   the spot-check purchase order's cost lines with Unleashed.
5. Add your team: **Team** → enter each person's name and email → **Add**. Each
   person gets a **temporary password**, shown once; send it to them along with the
   address. They choose their own password the first time they sign in.
   - **Members** see the dashboard. **Admins** can also manage the team and run
     connection checks.
   - Someone forgot their password? **Team** → **Reset password** gives them a new
     temporary one. Someone left? **Deactivate** signs them out immediately.
   - Locked out of every admin account? Set `ADMIN_EMAIL`, a new temporary
     `ADMIN_PASSWORD`, and `ADMIN_RESET` = `1` in the environment properties →
     **Apply** → sign in → then remove `ADMIN_RESET` and `ADMIN_PASSWORD`.

---

## Afterwards

- **Updating the app:** on your computer `git pull`, then
  `git archive -o unleashedaccess.zip HEAD`; in Elastic Beanstalk → your
  environment → **Upload and deploy** → choose the zip, label `v2` (then `v3`…).
- **Changing a password or key:** edit the environment property and **Apply**.
  If you change the database password in RDS (**Modify** → new master password),
  update `DB_PASSWORD` right after.
- **Logs:** Elastic Beanstalk → **Logs**, or CloudWatch Logs if you turned on streaming.
- **Cost:** a `db.t4g.micro` database, one `t3.small` instance and a load balancer
  run in the tens of dollars a month (less while the RDS free tier applies). The AWS
  Pricing Calculator gives exact figures for your region.
- **Removing it:** delete the Elastic Beanstalk environment, then the database
  (turn off deletion protection first), then the two security groups.

(function(){
    const page=document.getElementById("teamMapPage");
    const teamStatus=document.getElementById("teamStatus");
    const teamList=document.getElementById("teamList");
    const dataUrl=page ? page.dataset.teamUrl : "/team-map/data";
    const markers=new Map();
    let teamMap=null;

    function setStatus(text){
        if(teamStatus)teamStatus.textContent=text;
    }

    function escapeHtml(value){
        return String(value||"").replace(/[&<>"']/g,function(ch){
            return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch];
        });
    }

    function markerHtml(worker){
        const accuracy=worker.accuracy ? "<br>Accuracy: "+Math.round(worker.accuracy)+" m" : "";
        return "<strong>"+escapeHtml(worker.name)+"</strong><br>"+
            escapeHtml(worker.project_name)+"<br>"+
            escapeHtml(worker.source)+"<br>"+
            "Clock in: "+escapeHtml(worker.clock_in_time)+"<br>"+
            "Last seen: "+escapeHtml(worker.last_seen)+
            accuracy+"<br>"+escapeHtml(worker.address);
    }

    function listHtml(worker){
        const address=worker.address ? "<p>"+escapeHtml(worker.address)+"</p>" : "";
        return "<div class=\"teamPerson\"><strong>"+escapeHtml(worker.name)+"</strong><br>"+
            "<span class=\"muted\">"+escapeHtml(worker.email)+"</span>"+
            "<p>"+escapeHtml(worker.project_name)+"</p>"+
            "<p>"+escapeHtml(worker.source)+": "+escapeHtml(worker.last_seen)+"</p>"+
            address+"</div>";
    }

    function ensureMap(){
        if(teamMap)return true;
        if(typeof L==="undefined"){
            setStatus("Map library could not load. Check internet access and refresh.");
            return false;
        }
        teamMap=L.map("teamMap").setView([39.5,-98.35],4);
        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{
            maxZoom:19,
            attribution:"&copy; OpenStreetMap"
        }).addTo(teamMap);
        return true;
    }

    window.loadTeam=async function(forceFit){
        if(!ensureMap())return;
        try{
            const res=await fetch(dataUrl,{cache:"no-store"});
            if(!res.ok){
                setStatus("Team locations are temporarily unavailable.");
                return;
            }
            const data=await res.json();
            if(data.error){
                setStatus(data.error);
                if(teamList)teamList.innerHTML="";
                return;
            }
            const workers=data.workers||[];
            const seen=new Set();
            setStatus(workers.length ? workers.length+" active worker(s). Last refresh "+data.updated_at+"." : "No active clocked-in workers.");
            if(teamList)teamList.innerHTML=workers.map(listHtml).join("");
            const bounds=[];
            workers.forEach(function(worker){
                seen.add(String(worker.user_id));
                const pos=[worker.latitude,worker.longitude];
                bounds.push(pos);
                if(!markers.has(String(worker.user_id))){
                    markers.set(String(worker.user_id),L.marker(pos).addTo(teamMap));
                }else{
                    markers.get(String(worker.user_id)).setLatLng(pos);
                }
                markers.get(String(worker.user_id)).bindPopup(markerHtml(worker));
            });
            markers.forEach(function(marker,id){
                if(!seen.has(id)){
                    teamMap.removeLayer(marker);
                    markers.delete(id);
                }
            });
            if(bounds.length && (forceFit || markers.size===workers.length)){
                teamMap.fitBounds(bounds,{padding:[40,40],maxZoom:16});
            }
        }catch(e){
            setStatus("Could not load team locations.");
        }
    };

    window.loadTeam(true);
    setInterval(function(){window.loadTeam(false);},30000);
})();

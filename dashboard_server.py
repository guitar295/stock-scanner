// MOBILE SWIPE TO CLOSE POPUP (cả 2 chiều: trái→phải và phải→trái)
(function(){
  if(window.innerWidth > 768) return;
  const pbox = document.querySelector('.pbox');
  let startX=0, startY=0, dir='', fired=false, edgeSide='';

  pbox.addEventListener('touchstart', function(e){
    if(!document.getElementById('overlay').classList.contains('on')) return;
    if(lb.el && lb.el.classList.contains('on')) return;
    const x = e.touches[0].clientX;
    const W = window.innerWidth;
    // Nhận từ cạnh trái 40px HOẶC cạnh phải 40px
    if(x <= 40)          edgeSide = 'left';
    else if(x >= W - 40) edgeSide = 'right';
    else return;
    startX = x;
    startY = e.touches[0].clientY;
    dir = ''; fired = false;
  }, {passive:true});

  pbox.addEventListener('touchmove', function(e){
    if(fired || !edgeSide) return;
    const dx = e.touches[0].clientX - startX;
    const dy = e.touches[0].clientY - startY;
    if(!dir && (Math.abs(dx)>10 || Math.abs(dy)>10))
      dir = Math.abs(dx) > Math.abs(dy) ? 'h' : 'v';
    if(dir === 'h'){
      // Cạnh trái: vuốt sang phải (dx > 40)
      // Cạnh phải: vuốt sang trái (dx < -40)
      if((edgeSide === 'left' && dx > 40) || (edgeSide === 'right' && dx < -40)){
        fired = true;
        closePopup();
      }
    }
  }, {passive:true});

  pbox.addEventListener('touchend', function(){
    edgeSide = '';
  }, {passive:true});
})();
